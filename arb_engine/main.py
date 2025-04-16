"""
Arbitrage Engine Microservice for Crypto Arbitrage Bot.
Identifies arbitrage opportunities using graph-based algorithms.
"""
import asyncio
import logging
import time
from datetime import datetime
import networkx as nx
import pandas as pd
from fastapi import FastAPI, BackgroundTasks, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
import uvicorn

from common.config import (
    EXCHANGES, TOP_TRADING_PAIRS, ARB_ENGINE_PORT,
    MIN_PROFIT_MARGIN, MAX_CAPITAL_PER_TRADE, MIN_24H_VOLUME,
    MAX_BID_ASK_SPREAD, VOLATILITY_THRESHOLD, VOLATILITY_WINDOW
)
from common.redis_utils import (
    get_ticker, get_all_tickers, get_exchange_status,
    update_system_metric, cache_arbitrage_opportunity
)
from common.database import (
    get_session, add_arbitrage_opportunity, update_opportunity_status
)

# Setup logging
logger = logging.getLogger("arb_engine")

# Initialize FastAPI app
app = FastAPI(title="Arbitrage Engine Service")

# Price graph for arbitrage detection
price_graph = nx.DiGraph()

# Historical price data for volatility calculation
price_history = {}

# Opportunity tracking
active_opportunities = {}

def calculate_effective_price(price, exchange, is_buy=True):
    """Calculate effective price including fees and slippage."""
    exchange_config = EXCHANGES.get(exchange, {})
    
    # Get fee rate based on taker/maker
    fee_rate = exchange_config.get('taker_fee', 0.001) if is_buy else exchange_config.get('maker_fee', 0.0008)
    
    # Add estimated slippage (0.05% for liquid markets)
    slippage = 0.0005
    
    # Calculate effective price
    if is_buy:
        # When buying, effective price is higher due to fees and slippage
        effective_price = price * (1 + fee_rate + slippage)
    else:
        # When selling, effective price is lower due to fees and slippage
        effective_price = price * (1 - fee_rate - slippage)
    
    return effective_price

def check_volatility(pair, current_price):
    """Check if price volatility is within acceptable limits."""
    if pair not in price_history:
        price_history[pair] = []
    
    # Add current price to history
    timestamp = time.time()
    price_history[pair].append((timestamp, current_price))
    
    # Remove old prices
    cutoff_time = timestamp - VOLATILITY_WINDOW
    price_history[pair] = [(t, p) for t, p in price_history[pair] if t >= cutoff_time]
    
    # If we don't have enough history, assume volatility is acceptable
    if len(price_history[pair]) < 2:
        return True
    
    # Calculate volatility as max percentage change
    prices = [p for _, p in price_history[pair]]
    min_price = min(prices)
    max_price = max(prices)
    
    if min_price <= 0:
        return False
    
    volatility = (max_price - min_price) / min_price
    
    # Return True if volatility is within threshold
    return volatility <= VOLATILITY_THRESHOLD

async def check_liquidity(exchange, pair):
    """Check if pair has sufficient liquidity."""
    try:
        # Get ticker data
        ticker = await get_ticker(exchange, pair)
        if not ticker:
            return False
        
        # Check bid/ask spread
        bid = ticker.get('bid', 0)
        ask = ticker.get('ask', 0)
        
        if bid <= 0 or ask <= 0:
            return False
        
        spread = (ask - bid) / bid
        if spread > MAX_BID_ASK_SPREAD:
            return False
        
        # For a more comprehensive check, we would also verify 24h volume
        # This would require additional API calls to exchanges
        # For simplicity, we'll assume volume requirement is met if spread is acceptable
        
        return True
    
    except Exception as e:
        logger.error(f"Error checking liquidity for {exchange} {pair}: {str(e)}")
        return False

async def update_price_graph():
    """Update price graph with latest market data."""
    # Clear existing graph
    price_graph.clear()
    
    # Add nodes for each currency
    currencies = set()
    for pair in TOP_TRADING_PAIRS:
        base, quote = pair.split('/')
        currencies.add(base)
        currencies.add(quote)
    
    for currency in currencies:
        price_graph.add_node(currency)
    
    # Add edges for each exchange-pair combination
    for exchange in EXCHANGES:
        # Check if exchange is connected
        status = await get_exchange_status(exchange)
        if status.get('status') != 'connected':
            continue
        
        for pair in TOP_TRADING_PAIRS:
            # Get ticker data
            ticker = await get_ticker(exchange, pair)
            if not ticker:
                continue
            
            base, quote = pair.split('/')
            
            # Check liquidity
            if not await check_liquidity(exchange, pair):
                continue
            
            # Add buy edge (quote -> base)
            buy_price = ticker.get('ask', 0)
            if buy_price > 0:
                effective_buy_price = calculate_effective_price(buy_price, exchange, is_buy=True)
                price_graph.add_edge(
                    quote, base,
                    exchange=exchange,
                    price=buy_price,
                    effective_price=effective_buy_price,
                    action="buy"
                )
            
            # Add sell edge (base -> quote)
            sell_price = ticker.get('bid', 0)
            if sell_price > 0:
                effective_sell_price = calculate_effective_price(sell_price, exchange, is_buy=False)
                price_graph.add_edge(
                    base, quote,
                    exchange=exchange,
                    price=sell_price,
                    effective_price=effective_sell_price,
                    action="sell"
                )
    
    # Log graph stats
    nodes = price_graph.number_of_nodes()
    edges = price_graph.number_of_edges()
    logger.info(f"Updated price graph: {nodes} nodes, {edges} edges")
    await update_system_metric("arb_engine", "graph_nodes", nodes)
    await update_system_metric("arb_engine", "graph_edges", edges)

def find_negative_cycles():
    """Find negative cycles in the price graph using Bellman-Ford algorithm."""
    # Convert prices to negative log for multiplicative arbitrage detection
    log_graph = nx.DiGraph()
    
    for u, v, data in price_graph.edges(data=True):
        # Skip edges with zero or negative prices
        if data['effective_price'] <= 0:
            continue
        
        # Use negative log of price for multiplicative arbitrage
        # For buy: 1/price (amount of base you get for 1 unit of quote)
        # For sell: price (amount of quote you get for 1 unit of base)
        if data['action'] == 'buy':
            weight = -1 * (1 / data['effective_price'])
        else:
            weight = -1 * data['effective_price']
        
        log_graph.add_edge(u, v, **data, weight=weight)
    
    # Find negative cycles
    cycles = []
    
    # Check from each node as potential source
    for source in log_graph.nodes():
        try:
            # Use Bellman-Ford to detect negative cycles
            distance = {node: float('inf') for node in log_graph.nodes()}
            predecessor = {node: None for node in log_graph.nodes()}
            distance[source] = 0
            
            # Relax edges
            for _ in range(len(log_graph.nodes()) - 1):
                for u, v, data in log_graph.edges(data=True):
                    if distance[u] + data['weight'] < distance[v]:
                        distance[v] = distance[u] + data['weight']
                        predecessor[v] = u
            
            # Check for negative cycles
            for u, v, data in log_graph.edges(data=True):
                if distance[u] + data['weight'] < distance[v]:
                    # Negative cycle detected
                    cycle = [v, u]
                    current = u
                    while predecessor[current] not in cycle:
                        current = predecessor[current]
                        cycle.append(current)
                    
                    # Get the cycle in correct order
                    idx = cycle.index(predecessor[current])
                    cycle = cycle[idx:] + cycle[:idx]
                    
                    # Add to cycles list if not already present
                    if cycle not in cycles:
                        cycles.append(cycle)
        
        except Exception as e:
            logger.error(f"Error finding negative cycles from {source}: {str(e)}")
    
    return cycles

async def analyze_arbitrage_cycle(cycle):
    """Analyze an arbitrage cycle for profitability."""
    # Extract cycle details
    cycle_edges = []
    for i in range(len(cycle)):
        u = cycle[i]
        v = cycle[(i + 1) % len(cycle)]
        
        # Find the edge with the best price if multiple exchanges offer the same pair
        best_edge = None
        best_price = 0
        
        for _, _, data in price_graph.edges(data=True):
            if data['action'] == 'buy' and u == data['quote'] and v == data['base']:
                if best_edge is None or data['effective_price'] < best_price:
                    best_edge = data
                    best_price = data['effective_price']
            elif data['action'] == 'sell' and u == data['base'] and v == data['quote']:
                if best_edge is None or data['effective_price'] > best_price:
                    best_edge = data
                    best_price = data['effective_price']
        
        if best_edge:
            cycle_edges.append((u, v, best_edge))
    
    # Calculate profit margin
    profit_margin = 1.0
    start_currency = cycle[0]
    current_amount = 1.0
    
    for u, v, data in cycle_edges:
        if data['action'] == 'buy':
            # Convert quote to base: amount / price
            current_amount = current_amount / data['effective_price']
        else:
            # Convert base to quote: amount * price
            current_amount = current_amount * data['effective_price']
    
    # Calculate profit margin
    profit_margin = current_amount - 1.0
    
    # Check if profit margin meets minimum threshold
    if profit_margin < MIN_PROFIT_MARGIN:
        return None
    
    # Check volatility for all currencies in the cycle
    for currency in cycle:
        for pair in TOP_TRADING_PAIRS:
            base, quote = pair.split('/')
            if currency in (base, quote):
                # Get average price across exchanges
                tickers = await get_all_tickers(pair)
                if not tickers:
                    continue
                
                prices = [ticker.get('bid', 0) + ticker.get('ask', 0) / 2 for ticker in tickers.values()]
                avg_price = sum(prices) / len(prices) if prices else 0
                
                if avg_price <= 0 or not check_volatility(pair, avg_price):
                    logger.info(f"Rejecting opportunity due to high volatility in {pair}")
                    return None
    
    # Format the opportunity
    exchanges = [data['exchange'] for _, _, data in cycle_edges]
    pairs = []
    for i in range(len(cycle_edges)):
        u, v, data = cycle_edges[i]
        if data['action'] == 'buy':
            pairs.append(f"{v}/{u}")
        else:
            pairs.append(f"{u}/{v}")
    
    # Determine the main pair for reporting
    # Use the pair with the highest USD value if available
    main_pair = None
    for pair in pairs:
        if 'USDT' in pair or 'USD' in pair:
            main_pair = pair
            break
    
    if not main_pair and pairs:
        main_pair = pairs[0]
    
    # Calculate estimated volume
    # For simplicity, we'll use a fixed percentage of available capital
    volume = 1000.0  # Placeholder, would be calculated based on available balance
    
    return {
        "cycle": cycle,
        "edges": cycle_edges,
        "profit_margin": profit_margin,
        "exchanges": exchanges,
        "pairs": pairs,
        "main_pair": main_pair,
        "volume": volume,
        "timestamp": datetime.utcnow().isoformat()
    }

async def detect_arbitrage_opportunities():
    """Detect and analyze arbitrage opportunities."""
    try:
        # Update price graph
        await update_price_graph()
        
        # Find negative cycles
        cycles = find_negative_cycles()
        
        # Analyze each cycle
        opportunities = []
        for cycle in cycles:
            opportunity = await analyze_arbitrage_cycle(cycle)
            if opportunity:
                opportunities.append(opportunity)
        
        # Log stats
        await update_system_metric("arb_engine", "opportunities_found", len(opportunities))
        
        # Process opportunities
        for opportunity in opportunities:
            await process_arbitrage_opportunity(opportunity)
    
    except Exception as e:
        logger.error(f"Error detecting arbitrage opportunities: {str(e)}")

async def process_arbitrage_opportunity(opportunity, session: AsyncSession = None):
    """Process and store an arbitrage opportunity."""
    try:
        # Extract key details
        main_pair = opportunity.get('main_pair', '')
        if not main_pair:
            return
        
        exchanges = opportunity.get('exchanges', [])
        if len(exchanges) < 2:
            return
        
        buy_exchange = exchanges[0]
        sell_exchange = exchanges[-1]
        
        # For triangular arbitrage, both exchanges might be the same
        is_triangular = buy_exchange == sell_exchange
        
        # Get prices
        cycle_edges = opportunity.get('edges', [])
        buy_price = cycle_edges[0][2]['price'] if cycle_edges else 0
        sell_price = cycle_edges[-1][2]['price'] if cycle_edges else 0
        
        # Calculate volume
        volume = opportunity.get('volume', 0)
        profit_margin = opportunity.get('profit_margin', 0)
        
        # Create database session if not provided
        close_session = False
        if session is None:
            session = AsyncSession()
            close_session = True
        
        try:
            # Store in database
            db_opportunity = await add_arbitrage_opportunity(
                session=session,
                pair=main_pair,
                buy_exchange=buy_exchange,
                sell_exchange=sell_exchange,
                buy_price=buy_price,
                sell_price=sell_price,
                volume=volume,
                profit_margin=profit_margin
            )
            
            # Cache in Redis for quick access
            await cache_arbitrage_opportunity(
                opportunity_id=db_opportunity.id,
                data=opportunity
            )
            
            # Add to active opportunities
            active_opportunities[db_opportunity.id] = {
                "opportunity": opportunity,
                "db_id": db_opportunity.id,
                "status": "DETECTED",
                "timestamp": datetime.utcnow().isoformat()
            }
            
            logger.info(f"Stored arbitrage opportunity: {main_pair} with {profit_margin:.4f} profit margin")
            
            # Return the database ID
            return db_opportunity.id
        
        finally:
            if close_session:
                await session.close()
    
    except Exception as e:
        logger.error(f"Error processing arbitrage opportunity: {str(e)}")
        return None

async def arbitrage_scanner():
    """Background task to continuously scan for arbitrage opportunities."""
    while True:
        start_time = time.time()
        
        # Detect opportunities
        await detect_arbitrage_opportunities()
        
        # Calculate cycle time
        cycle_time = time.time() - start_time
        await update_system_metric("arb_engine", "scan_cycle_time", cycle_time)
        
        # Sleep to maintain target frequency (aim for 5 scans per second)
        sleep_time = max(0.2 - cycle_time, 0)
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)

@app.on_event("startup")
async def startup_event():
    """Initialize and start background tasks."""
    # Start arbitrage scanner
    asyncio.create_task(arbitrage_scanner())

@app.get("/opportunities")
async def get_opportunities(limit: int = 10, session: AsyncSession = Depends(get_session)):
    """Get recent arbitrage opportunities."""
    return {
        "opportunities": list(active_opportunities.values())[-limit:],
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/opportunity/{opportunity_id}")
async def get_opportunity(opportunity_id: int, session: AsyncSession = Depends(get_session)):
    """Get details of a specific arbitrage opportunity."""
    if opportunity_id in active_opportunities:
        return active_opportunities[opportunity_id]
    
    raise HTTPException(status_code=404, detail="Opportunity not found")

@app.post("/opportunity/{opportunity_id}/execute")
async def execute_opportunity(opportunity_id: int, session: AsyncSession = Depends(get_session)):
    """Mark an opportunity for execution."""
    if opportunity_id not in active_opportunities:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    
    # Update status
    active_opportunities[opportunity_id]["status"] = "EXECUTING"
    await update_opportunity_status(session, opportunity_id, "EXECUTING")
    
    return {"status": "executing", "opportunity_id": opportunity_id}

@app.post("/opportunity/{opportunity_id}/cancel")
async def cancel_opportunity(opportunity_id: int, session: AsyncSession = Depends(get_session)):
    """Cancel an opportunity."""
    if opportunity_id not in active_opportunities:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    
    # Update status
    active_opportunities[opportunity_id]["status"] = "CANCELED"
    await update_opportunity_status(session, opportunity_id, "CANCELED")
    
    return {"status": "canceled", "opportunity_id": opportunity_id}

@app.get("/status")
async def get_status():
    """Get service status."""
    return {
        "status": "running",
        "active_opportunities": len(active_opportunities),
        "timestamp": datetime.utcnow().isoformat()
    }

if __name__ == "__main__":
    uvicorn.run("arb_engine.main:app", host="0.0.0.0", port=ARB_ENGINE_PORT, reload=False)
