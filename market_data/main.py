"""
Market Data Microservice for Crypto Arbitrage Bot.
Handles WebSocket connections to exchanges and updates Redis cache with market data.
"""
import asyncio
import logging
import time
from datetime import datetime
import ccxt.async_support as ccxt
from fastapi import FastAPI, BackgroundTasks
import uvicorn

from common.config import EXCHANGES, TOP_TRADING_PAIRS, MARKET_DATA_PORT
from common.redis_utils import (
    update_ticker, update_orderbook, update_exchange_status,
    update_system_metric
)

# Setup logging
logger = logging.getLogger("market_data")

# Initialize FastAPI app
app = FastAPI(title="Market Data Service")

# Exchange connection instances
exchange_instances = {}

# Connection status tracking
connection_status = {}

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
        logger.info(f"Initialized {exchange_name} connection")
        
        # Update status
        await update_exchange_status(exchange_name, "connected")
        connection_status[exchange_name] = {
            "status": "connected",
            "last_update": time.time(),
            "error_count": 0
        }
        
        return exchange
    
    except Exception as e:
        logger.error(f"Error initializing {exchange_name}: {str(e)}")
        await update_exchange_status(exchange_name, "error", str(e))
        connection_status[exchange_name] = {
            "status": "error",
            "last_update": time.time(),
            "error_count": 1,
            "last_error": str(e)
        }
        return None

async def fetch_ticker_data(exchange_name, pair):
    """Fetch ticker data from exchange."""
    try:
        exchange = exchange_instances.get(exchange_name)
        if not exchange:
            exchange = await initialize_exchange(exchange_name)
            if not exchange:
                return
            exchange_instances[exchange_name] = exchange
        
        # Fetch ticker
        ticker = await exchange.fetch_ticker(pair)
        
        # Update Redis
        await update_ticker(
            exchange=exchange_name,
            pair=pair,
            bid=ticker['bid'],
            ask=ticker['ask'],
            timestamp=ticker['timestamp'] / 1000 if ticker['timestamp'] else None
        )
        
        # Update status
        connection_status[exchange_name]["last_update"] = time.time()
        connection_status[exchange_name]["error_count"] = 0
        
        # Log latency metric
        latency = time.time() - (ticker['timestamp'] / 1000 if ticker['timestamp'] else time.time())
        await update_system_metric("market_data", f"latency_{exchange_name}", latency)
        
        return ticker
    
    except Exception as e:
        logger.error(f"Error fetching ticker for {exchange_name} {pair}: {str(e)}")
        
        # Update error count
        if exchange_name in connection_status:
            connection_status[exchange_name]["error_count"] += 1
            connection_status[exchange_name]["last_error"] = str(e)
            
            # If too many errors, reset connection
            if connection_status[exchange_name]["error_count"] > 5:
                logger.warning(f"Too many errors for {exchange_name}, resetting connection")
                await close_exchange_connection(exchange_name)
        
        await update_exchange_status(exchange_name, "error", str(e))
        return None

async def fetch_orderbook_data(exchange_name, pair, limit=20):
    """Fetch orderbook data from exchange."""
    try:
        exchange = exchange_instances.get(exchange_name)
        if not exchange:
            exchange = await initialize_exchange(exchange_name)
            if not exchange:
                return
            exchange_instances[exchange_name] = exchange
        
        # Fetch orderbook
        orderbook = await exchange.fetch_order_book(pair, limit)
        
        # Update Redis
        await update_orderbook(
            exchange=exchange_name,
            pair=pair,
            bids=orderbook['bids'],
            asks=orderbook['asks'],
            timestamp=orderbook['timestamp'] / 1000 if orderbook['timestamp'] else None
        )
        
        # Update status
        connection_status[exchange_name]["last_update"] = time.time()
        connection_status[exchange_name]["error_count"] = 0
        
        return orderbook
    
    except Exception as e:
        logger.error(f"Error fetching orderbook for {exchange_name} {pair}: {str(e)}")
        
        # Update error count
        if exchange_name in connection_status:
            connection_status[exchange_name]["error_count"] += 1
            connection_status[exchange_name]["last_error"] = str(e)
        
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

async def monitor_connections():
    """Monitor exchange connections and restart if needed."""
    while True:
        current_time = time.time()
        for exchange_name, status in connection_status.items():
            # Check if connection is stale (no updates for 60 seconds)
            if current_time - status["last_update"] > 60:
                logger.warning(f"Connection to {exchange_name} is stale, restarting")
                await close_exchange_connection(exchange_name)
                # Connection will be reinitialized on next data fetch
        
        # Sleep for 30 seconds
        await asyncio.sleep(30)

async def fetch_all_tickers():
    """Fetch ticker data for all exchanges and pairs."""
    exchanges = list(EXCHANGES.keys())
    
    while True:
        start_time = time.time()
        
        # Create tasks for all exchange-pair combinations
        tasks = []
        for exchange_name in exchanges:
            for pair in TOP_TRADING_PAIRS:
                # Check if pair is supported by exchange
                exchange = exchange_instances.get(exchange_name)
                if not exchange:
                    continue
                
                if pair in exchange.markets:
                    tasks.append(fetch_ticker_data(exchange_name, pair))
        
        # Execute tasks concurrently
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        
        # Calculate cycle time and sleep if needed
        cycle_time = time.time() - start_time
        await update_system_metric("market_data", "ticker_cycle_time", cycle_time)
        
        # Target 100ms update frequency, sleep if cycle completed faster
        sleep_time = max(0.1 - cycle_time, 0)
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)

async def fetch_all_orderbooks():
    """Fetch orderbook data for all exchanges and pairs."""
    exchanges = list(EXCHANGES.keys())
    
    while True:
        start_time = time.time()
        
        # Create tasks for all exchange-pair combinations
        tasks = []
        for exchange_name in exchanges:
            for pair in TOP_TRADING_PAIRS:
                # Check if pair is supported by exchange
                exchange = exchange_instances.get(exchange_name)
                if not exchange:
                    continue
                
                if pair in exchange.markets:
                    tasks.append(fetch_orderbook_data(exchange_name, pair))
        
        # Execute tasks concurrently
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        
        # Calculate cycle time and sleep if needed
        cycle_time = time.time() - start_time
        await update_system_metric("market_data", "orderbook_cycle_time", cycle_time)
        
        # Target 1s update frequency for orderbooks, sleep if cycle completed faster
        sleep_time = max(1.0 - cycle_time, 0)
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)

@app.on_event("startup")
async def startup_event():
    """Initialize connections and start background tasks."""
    # Start background tasks
    asyncio.create_task(monitor_connections())
    asyncio.create_task(fetch_all_tickers())
    asyncio.create_task(fetch_all_orderbooks())

@app.on_event("shutdown")
async def shutdown_event():
    """Close all connections."""
    for exchange_name in list(exchange_instances.keys()):
        await close_exchange_connection(exchange_name)

@app.get("/status")
async def get_status():
    """Get service status."""
    return {
        "status": "running",
        "exchanges": connection_status,
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/exchanges")
async def get_exchanges():
    """Get list of connected exchanges."""
    return {
        "exchanges": list(exchange_instances.keys()),
        "timestamp": datetime.utcnow().isoformat()
    }

@app.post("/refresh/{exchange}")
async def refresh_exchange(exchange: str, background_tasks: BackgroundTasks):
    """Refresh exchange connection."""
    background_tasks.add_task(close_exchange_connection, exchange)
    return {"status": "refreshing", "exchange": exchange}

if __name__ == "__main__":
    uvicorn.run("market_data.main:app", host="0.0.0.0", port=MARKET_DATA_PORT, reload=False)
