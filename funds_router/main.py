"""
Funds Router Microservice for Crypto Arbitrage Bot.
Handles cross-exchange fund transfers and fee calculations.
"""
import asyncio
import logging
import time
from datetime import datetime
import ccxt.async_support as ccxt
from fastapi import FastAPI, BackgroundTasks, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
import uvicorn

from common.config import (
    EXCHANGES, FUNDS_ROUTER_PORT, MAX_TRANSFER_TIME
)
from common.redis_utils import (
    update_system_metric, acquire_lock, release_lock
)
from common.database import (
    get_session, add_fund_transfer, update_transfer_status
)

# Setup logging
logger = logging.getLogger("funds_router")

# Initialize FastAPI app
app = FastAPI(title="Funds Router Service")

# Exchange connection instances
exchange_instances = {}

# Active transfers tracking
active_transfers = {}

# Network fee estimates for different cryptocurrencies and networks
NETWORK_FEES = {
    "BTC": {
        "BTC": 0.0001,  # Native Bitcoin network
        "BSC": 0.0001,  # Binance Smart Chain
    },
    "ETH": {
        "ETH": 0.005,   # Native Ethereum network
        "BSC": 0.0005,  # Binance Smart Chain
        "Arbitrum": 0.0001,  # Arbitrum network
    },
    "USDT": {
        "ETH": 10,      # ERC-20
        "TRX": 1,       # TRC-20
        "BSC": 0.5,     # BEP-20
        "Arbitrum": 0.2,  # Arbitrum network
    },
    # Add more currencies and networks as needed
}

# Preferred networks for each currency (fastest and cheapest)
PREFERRED_NETWORKS = {
    "BTC": "BSC",
    "ETH": "Arbitrum",
    "USDT": "TRX",  # TRC-20 is usually fastest and cheapest
    # Add more currencies as needed
}

async def initialize_exchange(exchange_name):
    """Initialize exchange connection."""
    try:
        exchange_config = EXCHANGES.get(exchange_name, {})
        if not exchange_config:
            logger.error(f"Exchange {exchange_name} not configured")
            return None
        
        # Create exchange instance
        if exchange_name == "okx":
            exchange = ccxt.okx({
                'apiKey': exchange_config.get('api_key'),
                'secret': exchange_config.get('api_secret'),
                'password': exchange_config.get('password'),
                'enableRateLimit': True
            })
        elif exchange_name == "bybit":
            exchange = ccxt.bybit({
                'apiKey': exchange_config.get('api_key'),
                'secret': exchange_config.get('api_secret'),
                'enableRateLimit': True
            })
        elif exchange_name == "htx":
            exchange = ccxt.htx({
                'apiKey': exchange_config.get('api_key'),
                'secret': exchange_config.get('api_secret'),
                'enableRateLimit': True
            })
        else:
            # For DEXes or other exchanges
            logger.warning(f"Exchange {exchange_name} not directly supported, using generic implementation")
            exchange = getattr(ccxt, exchange_name)({
                'apiKey': exchange_config.get('api_key'),
                'secret': exchange_config.get('api_secret'),
                'enableRateLimit': True
            })
        
        # Load markets
        await exchange.load_markets()
        logger.info(f"Initialized {exchange_name} connection for funds routing")
        
        return exchange
    
    except Exception as e:
        logger.error(f"Error initializing {exchange_name} for funds routing: {str(e)}")
        return None

async def close_exchange_connection(exchange_name):
    """Close exchange connection."""
    try:
        exchange = exchange_instances.pop(exchange_name, None)
        if exchange:
            await exchange.close()
            logger.info(f"Closed {exchange_name} connection")
    except Exception as e:
        logger.error(f"Error closing {exchange_name} connection: {str(e)}")

async def get_balance(exchange_name, currency):
    """Get balance for a specific currency on an exchange."""
    try:
        # Get exchange instance
        exchange = exchange_instances.get(exchange_name)
        if not exchange:
            exchange = await initialize_exchange(exchange_name)
            if not exchange:
                return None
            exchange_instances[exchange_name] = exchange
        
        # Fetch balance
        balance = await exchange.fetch_balance()
        
        # Extract currency balance
        currency_balance = balance.get(currency, {})
        free = currency_balance.get('free', 0)
        used = currency_balance.get('used', 0)
        total = currency_balance.get('total', 0)
        
        return {
            "free": free,
            "used": used,
            "total": total
        }
    
    except Exception as e:
        logger.error(f"Error getting balance for {currency} on {exchange_name}: {str(e)}")
        return None

async def get_withdrawal_fee(exchange_name, currency, network=None):
    """Get withdrawal fee for a specific currency and network."""
    try:
        # Get exchange instance
        exchange = exchange_instances.get(exchange_name)
        if not exchange:
            exchange = await initialize_exchange(exchange_name)
            if not exchange:
                return None
            exchange_instances[exchange_name] = exchange
        
        # Fetch currencies
        currencies = await exchange.fetch_currencies()
        
        # Find currency
        currency_info = currencies.get(currency, {})
        
        # Extract networks
        networks = currency_info.get('networks', {})
        
        # If network is specified, get fee for that network
        if network and network in networks:
            fee = networks[network].get('fee', 0)
            return fee
        
        # If network is not specified, find the cheapest network
        min_fee = float('inf')
        for net_id, net_info in networks.items():
            fee = net_info.get('fee', 0)
            if fee < min_fee:
                min_fee = fee
                network = net_id
        
        if min_fee == float('inf'):
            # If no fee information is available, use estimates
            if currency in NETWORK_FEES:
                if network in NETWORK_FEES[currency]:
                    min_fee = NETWORK_FEES[currency][network]
                elif PREFERRED_NETWORKS.get(currency) in NETWORK_FEES[currency]:
                    min_fee = NETWORK_FEES[currency][PREFERRED_NETWORKS[currency]]
                else:
                    # Use first available network
                    for net, fee in NETWORK_FEES[currency].items():
                        min_fee = fee
                        break
        
        return min_fee
    
    except Exception as e:
        logger.error(f"Error getting withdrawal fee for {currency} on {exchange_name}: {str(e)}")
        
        # Use estimated fees if API call fails
        if currency in NETWORK_FEES:
            if network in NETWORK_FEES[currency]:
                return NETWORK_FEES[currency][network]
            elif PREFERRED_NETWORKS.get(currency) in NETWORK_FEES[currency]:
                return NETWORK_FEES[currency][PREFERRED_NETWORKS[currency]]
        
        return 0

async def withdraw_funds(exchange_name, currency, amount, address, network=None):
    """Withdraw funds from an exchange."""
    try:
        # Get exchange instance
        exchange = exchange_instances.get(exchange_name)
        if not exchange:
            exchange = await initialize_exchange(exchange_name)
            if not exchange:
                return None
            exchange_instances[exchange_name] = exchange
        
        # Determine network
        if not network and currency in PREFERRED_NETWORKS:
            network = PREFERRED_NETWORKS[currency]
        
        # Prepare parameters
        params = {}
        if network:
            params['network'] = network
        
        # Execute withdrawal
        withdrawal = await exchange.withdraw(currency, amount, address, tag=None, params=params)
        
        logger.info(f"Withdrew {amount} {currency} from {exchange_name} to {address} via {network}")
        return withdrawal
    
    except Exception as e:
        logger.error(f"Error withdrawing {amount} {currency} from {exchange_name}: {str(e)}")
        return None

async def get_deposit_address(exchange_name, currency, network=None):
    """Get deposit address for a specific currency on an exchange."""
    try:
        # Get exchange instance
        exchange = exchange_instances.get(exchange_name)
        if not exchange:
            exchange = await initialize_exchange(exchange_name)
            if not exchange:
                return None
            exchange_instances[exchange_name] = exchange
        
        # Determine network
        if not network and currency in PREFERRED_NETWORKS:
            network = PREFERRED_NETWORKS[currency]
        
        # Prepare parameters
        params = {}
        if network:
            params['network'] = network
        
        # Fetch deposit address
        address_info = await exchange.fetch_deposit_address(currency, params)
        
        return address_info
    
    except Exception as e:
        logger.error(f"Error getting deposit address for {currency} on {exchange_name}: {str(e)}")
        return None

async def check_transaction_status(exchange_name, transaction_id, currency):
    """Check the status of a withdrawal transaction."""
    try:
        # Get exchange instance
        exchange = exchange_instances.get(exchange_name)
        if not exchange:
            exchange = await initialize_exchange(exchange_name)
            if not exchange:
                return None
            exchange_instances[exchange_name] = exchange
        
        # Fetch withdrawals
        since = int((datetime.utcnow().timestamp() - 86400) * 1000)  # Last 24 hours
        withdrawals = await exchange.fetch_withdrawals(currency, since)
        
        # Find transaction
        for withdrawal in withdrawals:
            if withdrawal['id'] == transaction_id:
                return withdrawal
        
        return None
    
    except Exception as e:
        logger.error(f"Error checking transaction status for {transaction_id} on {exchange_name}: {str(e)}")
        return None

async def transfer_funds(from_exchange, to_exchange, currency, amount, network=None, session=None):
    """Transfer funds between exchanges."""
    # Generate a unique transfer ID
    transfer_id = f"transfer_{int(time.time())}_{from_exchange}_{to_exchange}_{currency}"
    
    # Acquire lock to prevent concurrent transfers
    lock_id = await acquire_lock(f"transfer_{from_exchange}_{currency}")
    if not lock_id:
        logger.error(f"Could not acquire lock for {from_exchange} {currency}")
        return None
    
    try:
        # Create database session if not provided
        close_session = False
        if session is None:
            session = AsyncSession()
            close_session = True
        
        try:
            # Check balance on source exchange
            balance = await get_balance(from_exchange, currency)
            if not balance or balance.get('free', 0) < amount:
                logger.error(f"Insufficient balance of {currency} on {from_exchange}")
                return None
            
            # Get withdrawal fee
            withdrawal_fee = await get_withdrawal_fee(from_exchange, currency, network)
            
            # Get deposit address on destination exchange
            address_info = await get_deposit_address(to_exchange, currency, network)
            if not address_info or not address_info.get('address'):
                logger.error(f"Could not get deposit address for {currency} on {to_exchange}")
                return None
            
            address = address_info.get('address')
            tag = address_info.get('tag')
            
            # Record transfer in database
            db_transfer = await add_fund_transfer(
                session=session,
                from_exchange=from_exchange,
                to_exchange=to_exchange,
                currency=currency,
                amount=amount,
                fee=withdrawal_fee
            )
            
            # Execute withdrawal
            withdrawal = await withdraw_funds(from_exchange, currency, amount, address, network)
            if not withdrawal:
                logger.error(f"Failed to withdraw {amount} {currency} from {from_exchange}")
                await update_transfer_status(session, db_transfer.id, "FAILED")
                return None
            
            transaction_id = withdrawal.get('id')
            
            # Update transfer record
            await update_transfer_status(session, db_transfer.id, "PENDING", transaction_id)
            
            # Track transfer
            active_transfers[db_transfer.id] = {
                "from_exchange": from_exchange,
                "to_exchange": to_exchange,
                "currency": currency,
                "amount": amount,
                "network": network,
                "transaction_id": transaction_id,
                "status": "PENDING",
                "timestamp": datetime.utcnow().isoformat()
            }
            
            logger.info(f"Initiated transfer of {amount} {currency} from {from_exchange} to {to_exchange}")
            
            # Start monitoring transfer status
            asyncio.create_task(monitor_transfer_status(db_transfer.id, from_exchange, transaction_id, currency))
            
            return db_transfer.id
        
        finally:
            if close_session:
                await session.close()
    
    finally:
        # Release lock
        await release_lock(f"transfer_{from_exchange}_{currency}", lock_id)

async def monitor_transfer_status(transfer_id, exchange_name, transaction_id, currency):
    """Monitor the status of a fund transfer."""
    start_time = time.time()
    completed = False
    
    while time.time() - start_time < MAX_TRANSFER_TIME and not completed:
        try:
            # Check transaction status
            transaction = await check_transaction_status(exchange_name, transaction_id, currency)
            
            if transaction:
                status = transaction.get('status', 'pending')
                
                # Update transfer status
                if status == 'ok' or status == 'completed':
                    async with AsyncSession() as session:
                        await update_transfer_status(session, transfer_id, "COMPLETED")
                    
                    if transfer_id in active_transfers:
                        active_transfers[transfer_id]["status"] = "COMPLETED"
                    
                    logger.info(f"Transfer {transfer_id} completed successfully")
                    completed = True
                
                elif status == 'failed' or status == 'canceled' or status == 'rejected':
                    async with AsyncSession() as session:
                        await update_transfer_status(session, transfer_id, "FAILED")
                    
                    if transfer_id in active_transfers:
                        active_transfers[transfer_id]["status"] = "FAILED"
                    
                    logger.error(f"Transfer {transfer_id} failed with status {status}")
                    completed = True
            
            # Sleep before checking again
            await asyncio.sleep(30)
        
        except Exception as e:
            logger.error(f"Error monitoring transfer {transfer_id}: {str(e)}")
            await asyncio.sleep(30)
    
    # If timeout occurred
    if not completed:
        logger.warning(f"Transfer {transfer_id} monitoring timed out after {MAX_TRANSFER_TIME} seconds")
        
        async with AsyncSession() as session:
            await update_transfer_status(session, transfer_id, "UNKNOWN")
        
        if transfer_id in active_transfers:
            active_transfers[transfer_id]["status"] = "UNKNOWN"

@app.on_event("startup")
async def startup_event():
    """Initialize connections."""
    pass

@app.on_event("shutdown")
async def shutdown_event():
    """Close all connections."""
    for exchange_name in list(exchange_instances.keys()):
        await close_exchange_connection(exchange_name)

@app.post("/transfer")
async def transfer_funds_endpoint(
    from_exchange: str,
    to_exchange: str,
    currency: str,
    amount: float,
    network: str = None,
    session: AsyncSession = Depends(get_session)
):
    """Transfer funds between exchanges."""
    transfer_id = await transfer_funds(from_exchange, to_exchange, currency, amount, network, session)
    
    if transfer_id:
        return {
            "status": "pending",
            "transfer_id": transfer_id,
            "from_exchange": from_exchange,
            "to_exchange": to_exchange,
            "currency": currency,
            "amount": amount,
            "network": network,
            "timestamp": datetime.utcnow().isoformat()
        }
    else:
        raise HTTPException(status_code=500, detail="Failed to initiate transfer")

@app.get("/transfers")
async def get_transfers():
    """Get all active transfers."""
    return {
        "transfers": active_transfers,
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/transfer/{transfer_id}")
async def get_transfer(transfer_id: int):
    """Get details of a specific transfer."""
    if str(transfer_id) in active_transfers:
        return active_transfers[str(transfer_id)]
    
    raise HTTPException(status_code=404, detail="Transfer not found")

@app.get("/balance/{exchange}/{currency}")
async def get_balance_endpoint(exchange: str, currency: str):
    """Get balance for a specific currency on an exchange."""
    balance = await get_balance(exchange, currency)
    
    if balance:
        return {
            "exchange": exchange,
            "currency": currency,
            "balance": balance,
            "timestamp": datetime.utcnow().isoformat()
        }
    else:
        raise HTTPException(status_code=500, detail="Failed to get balance")

@app.get("/fee/{exchange}/{currency}")
async def get_fee_endpoint(exchange: str, currency: str, network: str = None):
    """Get withdrawal fee for a specific currency and network."""
    fee = await get_withdrawal_fee(exchange, currency, network)
    
    return {
        "exchange": exchange,
        "currency": currency,
        "network": network,
        "fee": fee,
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/status")
async def get_status():
    """Get service status."""
    return {
        "status": "running",
        "active_transfers": len(active_transfers),
        "exchanges": list(exchange_instances.keys()),
        "timestamp": datetime.utcnow().isoformat()
    }

if __name__ == "__main__":
    uvicorn.run("funds_router.main:app", host="0.0.0.0", port=FUNDS_ROUTER_PORT, reload=False)
