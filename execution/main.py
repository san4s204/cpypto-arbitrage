"""
Execution Microservice for Crypto Arbitrage Bot.
Handles order routing, execution, and status monitoring.
"""
import asyncio
import logging
import time
import uuid
from datetime import datetime
import ccxt.async_support as ccxt
from fastapi import FastAPI, BackgroundTasks, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
import uvicorn

from common.config import (
    EXCHANGES, EXECUTION_PORT, decrypt_api_key
)
from common.redis_utils import (
    get_cached_opportunity, update_system_metric,
    get_ticker
)
from common.database import (
    get_session, update_opportunity_status,
    add_trade, update_trade_status
)

# Setup logging
logger = logging.getLogger("execution")

# Initialize FastAPI app
app = FastAPI(title="Execution Service")

# Exchange connection instances
exchange_instances = {}

# Active orders tracking
active_orders = {}

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
        logger.info(f"Initialized {exchange_name} connection for execution")
        
        return exchange
    
    except Exception as e:
        logger.error(f"Error initializing {exchange_name} for execution: {str(e)}")
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

async def place_order(exchange_name, pair, side, amount, price=None, order_type="limit"):
    """Place an order on the exchange."""
    try:
        # Get exchange instance
        exchange = exchange_instances.get(exchange_name)
        if not exchange:
            exchange = await initialize_exchange(exchange_name)
            if not exchange:
                return None
            exchange_instances[exchange_name] = exchange
        
        # Place order
        order_params = {}
        
        if order_type == "market":
            if side == "buy":
                order = await exchange.create_market_buy_order(pair, amount, order_params)
            else:
                order = await exchange.create_market_sell_order(pair, amount, order_params)
        else:
            if side == "buy":
                order = await exchange.create_limit_buy_order(pair, amount, price, order_params)
            else:
                order = await exchange.create_limit_sell_order(pair, amount, price, order_params)
        
        # Track order
        order_id = order.get('id')
        if order_id:
            active_orders[order_id] = {
                "exchange": exchange_name,
                "pair": pair,
                "side": side,
                "amount": amount,
                "price": price,
                "order_type": order_type,
                "status": order.get('status', 'open'),
                "timestamp": datetime.utcnow().isoformat()
            }
        
        logger.info(f"Placed {side} order on {exchange_name} for {amount} {pair} at {price}")
        return order
    
    except Exception as e:
        logger.error(f"Error placing order on {exchange_name}: {str(e)}")
        return None

async def check_order_status(exchange_name, order_id):
    """Check the status of an order."""
    try:
        # Get exchange instance
        exchange = exchange_instances.get(exchange_name)
        if not exchange:
            exchange = await initialize_exchange(exchange_name)
            if not exchange:
                return None
            exchange_instances[exchange_name] = exchange
        
        # Fetch order
        order = await exchange.fetch_order(order_id)
        
        # Update tracking
        if order_id in active_orders:
            active_orders[order_id]["status"] = order.get('status', 'unknown')
        
        return order
    
    except Exception as e:
        logger.error(f"Error checking order status on {exchange_name}: {str(e)}")
        return None

async def cancel_order(exchange_name, order_id):
    """Cancel an order."""
    try:
        # Get exchange instance
        exchange = exchange_instances.get(exchange_name)
        if not exchange:
            exchange = await initialize_exchange(exchange_name)
            if not exchange:
                return False
            exchange_instances[exchange_name] = exchange
        
        # Cancel order
        result = await exchange.cancel_order(order_id)
        
        # Update tracking
        if order_id in active_orders:
            active_orders[order_id]["status"] = "canceled"
        
        logger.info(f"Canceled order {order_id} on {exchange_name}")
        return True
    
    except Exception as e:
        logger.error(f"Error canceling order on {exchange_name}: {str(e)}")
        return False

async def execute_arbitrage_opportunity(opportunity_id, session):
    """Execute an arbitrage opportunity."""
    try:
        # Get opportunity details
        opportunity_data = await get_cached_opportunity(opportunity_id)
        if not opportunity_data:
            logger.error(f"Opportunity {opportunity_id} not found in cache")
            await update_opportunity_status(session, opportunity_id, "FAILED")
            return False
        
        # Update status
        await update_opportunity_status(session, opportunity_id, "EXECUTING")
        
        # Extract cycle details
        cycle_edges = opportunity_data.get('edges', [])
        if not cycle_edges:
            logger.error(f"No edges found in opportunity {opportunity_id}")
            await update_opportunity_status(session, opportunity_id, "FAILED")
            return False
        
        # Execute each step in the cycle
        orders = []
        for i, (u, v, data) in enumerate(cycle_edges):
            exchange = data.get('exchange')
            action = data.get('action')
            price = data.get('price')
            
            # Determine pair and side
            if action == 'buy':
                pair = f"{v}/{u}"  # base/quote
                side = "buy"
            else:
                pair = f"{u}/{v}"  # base/quote
                side = "sell"
            
            # Get current market price to ensure it's still profitable
            ticker = await get_ticker(exchange, pair)
            if not ticker:
                logger.error(f"Could not get ticker for {exchange} {pair}")
                # Cancel all previous orders
                for order_info in orders:
                    await cancel_order(order_info['exchange'], order_info['order_id'])
                await update_opportunity_status(session, opportunity_id, "FAILED")
                return False
            
            current_price = ticker.get('ask' if side == 'buy' else 'bid', 0)
            if current_price <= 0:
                logger.error(f"Invalid price for {exchange} {pair}")
                # Cancel all previous orders
                for order_info in orders:
                    await cancel_order(order_info['exchange'], order_info['order_id'])
                await update_opportunity_status(session, opportunity_id, "FAILED")
                return False
            
            # Check if price has moved unfavorably
            if (side == 'buy' and current_price > price * 1.005) or (side == 'sell' and current_price < price * 0.995):
                logger.warning(f"Price moved unfavorably for {exchange} {pair}")
                # Cancel all previous orders
                for order_info in orders:
                    await cancel_order(order_info['exchange'], order_info['order_id'])
                await update_opportunity_status(session, opportunity_id, "FAILED")
                return False
            
            # Determine amount
            # For simplicity, we'll use a fixed amount
            # In a real implementation, this would be calculated based on available balance
            amount = 0.01  # Placeholder
            
            # Place order
            order = await place_order(exchange, pair, side, amount, current_price)
            if not order:
                logger.error(f"Failed to place {side} order on {exchange} for {pair}")
                # Cancel all previous orders
                for order_info in orders:
                    await cancel_order(order_info['exchange'], order_info['order_id'])
                await update_opportunity_status(session, opportunity_id, "FAILED")
                return False
            
            # Store order in database
            order_id = order.get('id', str(uuid.uuid4()))
            fee = order.get('fee', {}).get('cost', 0)
            
            db_trade = await add_trade(
                session=session,
                opportunity_id=opportunity_id,
                exchange=exchange,
                pair=pair,
                side=side,
                price=current_price,
                amount=amount,
                fee=fee,
                order_id=order_id
            )
            
            # Track order
            orders.append({
                'exchange': exchange,
                'order_id': order_id,
                'trade_id': db_trade.id,
                'status': order.get('status', 'open')
            })
            
            # Wait for order to fill
            max_wait = 60  # seconds
            start_time = time.time()
            filled = False
            
            while time.time() - start_time < max_wait:
                order_status = await check_order_status(exchange, order_id)
                if not order_status:
                    await asyncio.sleep(1)
                    continue
                
                status = order_status.get('status')
                if status == 'closed':
                    filled = True
                    await update_trade_status(session, db_trade.id, "FILLED")
                    break
                elif status in ('canceled', 'expired', 'rejected'):
                    logger.error(f"Order {order_id} on {exchange} {status}")
                    # Cancel all previous orders
                    for order_info in orders:
                        if order_info['order_id'] != order_id:
                            await cancel_order(order_info['exchange'], order_info['order_id'])
                    await update_opportunity_status(session, opportunity_id, "FAILED")
                    await update_trade_status(session, db_trade.id, "FAILED")
                    return False
                
                await asyncio.sleep(1)
            
            if not filled:
                logger.error(f"Order {order_id} on {exchange} timed out")
                # Cancel the current order
                await cancel_order(exchange, order_id)
                # Cancel all previous orders
                for order_info in orders:
                    if order_info['order_id'] != order_id:
                        await cancel_order(order_info['exchange'], order_info['order_id'])
                await update_opportunity_status(session, opportunity_id, "FAILED")
                await update_trade_status(session, db_trade.id, "CANCELED")
                return False
        
        # All orders filled successfully
        await update_opportunity_status(session, opportunity_id, "COMPLETED")
        logger.info(f"Successfully executed arbitrage opportunity {opportunity_id}")
        
        # Calculate and log profit
        # In a real implementation, this would calculate actual profit based on executed prices
        profit_margin = opportunity_data.get('profit_margin', 0)
        await update_system_metric("execution", "profit_margin", profit_margin)
        
        return True
    
    except Exception as e:
        logger.error(f"Error executing arbitrage opportunity {opportunity_id}: {str(e)}")
        await update_opportunity_status(session, opportunity_id, "FAILED")
        return False

@app.on_event("startup")
async def startup_event():
    """Initialize connections."""
    pass

@app.on_event("shutdown")
async def shutdown_event():
    """Close all connections."""
    for exchange_name in list(exchange_instances.keys()):
        await close_exchange_connection(exchange_name)

@app.post("/execute/{opportunity_id}")
async def execute_opportunity(
    opportunity_id: int,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session)
):
    """Execute an arbitrage opportunity."""
    # Start execution in background
    background_tasks.add_task(execute_arbitrage_opportunity, opportunity_id, session)
    
    return {
        "status": "executing",
        "opportunity_id": opportunity_id,
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/orders")
async def get_orders():
    """Get all active orders."""
    return {
        "orders": active_orders,
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/order/{order_id}")
async def get_order(order_id: str):
    """Get details of a specific order."""
    if order_id in active_orders:
        return active_orders[order_id]
    
    raise HTTPException(status_code=404, detail="Order not found")

@app.post("/order/{order_id}/cancel")
async def cancel_order_endpoint(order_id: str):
    """Cancel an order."""
    if order_id not in active_orders:
        raise HTTPException(status_code=404, detail="Order not found")
    
    exchange = active_orders[order_id]["exchange"]
    result = await cancel_order(exchange, order_id)
    
    if result:
        return {"status": "canceled", "order_id": order_id}
    else:
        raise HTTPException(status_code=500, detail="Failed to cancel order")

@app.get("/status")
async def get_status():
    """Get service status."""
    return {
        "status": "running",
        "active_orders": len(active_orders),
        "exchanges": list(exchange_instances.keys()),
        "timestamp": datetime.utcnow().isoformat()
    }

if __name__ == "__main__":
    uvicorn.run("execution.main:app", host="0.0.0.0", port=EXECUTION_PORT, reload=False)
