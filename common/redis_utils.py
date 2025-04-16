"""
Redis utilities for the Crypto Arbitrage Bot.
Handles Redis connections and data caching.
"""
import json
import asyncio
import redis.asyncio as redis
from datetime import datetime

from common.config import (
    REDIS_HOST, REDIS_PORT, REDIS_DB, REDIS_PASSWORD
)

# Create Redis pool
redis_pool = redis.ConnectionPool(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=REDIS_DB,
    password=REDIS_PASSWORD,
    decode_responses=True
)

async def get_redis():
    """Get Redis connection from pool."""
    return redis.Redis(connection_pool=redis_pool)

# Market data keys
def market_data_key(exchange, pair, data_type="ticker"):
    """Generate Redis key for market data."""
    return f"market:{exchange}:{pair}:{data_type}"

# Market data functions
async def update_ticker(exchange, pair, bid, ask, timestamp=None):
    """Update ticker data in Redis."""
    if timestamp is None:
        timestamp = datetime.utcnow().timestamp()
    
    r = await get_redis()
    key = market_data_key(exchange, pair)
    data = {
        "bid": float(bid),
        "ask": float(ask),
        "timestamp": float(timestamp)
    }
    
    await r.hset(key, mapping=data)
    # Set expiry to 1 hour to prevent stale data accumulation
    await r.expire(key, 3600)
    
    # Also update the last update time for this exchange-pair
    await r.hset(f"last_update:{exchange}", pair, timestamp)

async def get_ticker(exchange, pair):
    """Get ticker data from Redis."""
    r = await get_redis()
    key = market_data_key(exchange, pair)
    data = await r.hgetall(key)
    
    if not data:
        return None
    
    return {
        "bid": float(data.get("bid", 0)),
        "ask": float(data.get("ask", 0)),
        "timestamp": float(data.get("timestamp", 0))
    }

async def get_all_tickers(pair):
    """Get ticker data for a specific pair across all exchanges."""
    r = await get_redis()
    # Get all keys matching the pattern
    keys = await r.keys(f"market:*:{pair}:ticker")
    
    result = {}
    for key in keys:
        # Extract exchange from key
        parts = key.split(":")
        if len(parts) >= 4:
            exchange = parts[1]
            data = await r.hgetall(key)
            if data:
                result[exchange] = {
                    "bid": float(data.get("bid", 0)),
                    "ask": float(data.get("ask", 0)),
                    "timestamp": float(data.get("timestamp", 0))
                }
    
    return result

async def update_orderbook(exchange, pair, bids, asks, timestamp=None):
    """Update orderbook data in Redis."""
    if timestamp is None:
        timestamp = datetime.utcnow().timestamp()
    
    r = await get_redis()
    key = market_data_key(exchange, pair, "orderbook")
    
    # Convert bids and asks to strings for Redis storage
    data = {
        "bids": json.dumps(bids),
        "asks": json.dumps(asks),
        "timestamp": float(timestamp)
    }
    
    await r.hset(key, mapping=data)
    # Set expiry to 1 hour
    await r.expire(key, 3600)

async def get_orderbook(exchange, pair):
    """Get orderbook data from Redis."""
    r = await get_redis()
    key = market_data_key(exchange, pair, "orderbook")
    data = await r.hgetall(key)
    
    if not data:
        return None
    
    return {
        "bids": json.loads(data.get("bids", "[]")),
        "asks": json.loads(data.get("asks", "[]")),
        "timestamp": float(data.get("timestamp", 0))
    }

# Exchange status tracking
async def update_exchange_status(exchange, status, message=None):
    """Update exchange connection status."""
    r = await get_redis()
    key = f"exchange:status:{exchange}"
    data = {
        "status": status,
        "timestamp": datetime.utcnow().timestamp()
    }
    if message:
        data["message"] = message
    
    await r.hset(key, mapping=data)

async def get_exchange_status(exchange):
    """Get exchange connection status."""
    r = await get_redis()
    key = f"exchange:status:{exchange}"
    data = await r.hgetall(key)
    
    if not data:
        return {"status": "unknown", "timestamp": 0}
    
    return data

# Arbitrage opportunity caching
async def cache_arbitrage_opportunity(opportunity_id, data, ttl=300):
    """Cache arbitrage opportunity data."""
    r = await get_redis()
    key = f"arbitrage:opportunity:{opportunity_id}"
    await r.set(key, json.dumps(data), ex=ttl)

async def get_cached_opportunity(opportunity_id):
    """Get cached arbitrage opportunity data."""
    r = await get_redis()
    key = f"arbitrage:opportunity:{opportunity_id}"
    data = await r.get(key)
    
    if not data:
        return None
    
    return json.loads(data)

# System metrics
async def update_system_metric(service, metric_name, value):
    """Update system metric in Redis."""
    r = await get_redis()
    key = f"metrics:{service}:{metric_name}"
    timestamp = datetime.utcnow().timestamp()
    
    # Store the last 100 values with timestamps for time-series data
    await r.lpush(key, json.dumps({"value": value, "timestamp": timestamp}))
    await r.ltrim(key, 0, 99)  # Keep only the last 100 entries
    
    # Also store the current value
    await r.set(f"{key}:current", value)

async def get_system_metric(service, metric_name):
    """Get current system metric value."""
    r = await get_redis()
    key = f"metrics:{service}:{metric_name}:current"
    value = await r.get(key)
    
    if value is None:
        return None
    
    return float(value)

async def get_system_metric_history(service, metric_name, limit=100):
    """Get system metric history."""
    r = await get_redis()
    key = f"metrics:{service}:{metric_name}"
    data = await r.lrange(key, 0, limit - 1)
    
    return [json.loads(item) for item in data]

# Lock mechanism for distributed operations
async def acquire_lock(lock_name, ttl=10):
    """Acquire a distributed lock."""
    r = await get_redis()
    lock_key = f"lock:{lock_name}"
    identifier = str(datetime.utcnow().timestamp())
    
    # Try to acquire the lock
    acquired = await r.set(lock_key, identifier, nx=True, ex=ttl)
    
    if acquired:
        return identifier
    
    return None

async def release_lock(lock_name, identifier):
    """Release a distributed lock."""
    r = await get_redis()
    lock_key = f"lock:{lock_name}"
    
    # Only release if we own the lock
    current = await r.get(lock_key)
    if current == identifier:
        await r.delete(lock_key)
        return True
    
    return False
