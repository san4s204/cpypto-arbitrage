"""
Database models and utilities for the Crypto Arbitrage Bot.
Handles database connections and ORM models.
"""
import asyncio
from datetime import datetime
import sqlalchemy as sa
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.future import select

from common.config import (
    POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB,
    POSTGRES_USER, POSTGRES_PASSWORD
)

# Create async engine
DATABASE_URL = f"postgresql+asyncpg://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Base class for all models
Base = declarative_base()

# Models
class ArbitrageOpportunity(Base):
    """Model for storing arbitrage opportunities."""
    __tablename__ = "arbitrage_opportunities"
    
    id = sa.Column(sa.Integer, primary_key=True)
    timestamp = sa.Column(sa.DateTime, default=datetime.utcnow)
    pair = sa.Column(sa.String(20), nullable=False)
    buy_exchange = sa.Column(sa.String(50), nullable=False)
    sell_exchange = sa.Column(sa.String(50), nullable=False)
    buy_price = sa.Column(sa.Float, nullable=False)
    sell_price = sa.Column(sa.Float, nullable=False)
    volume = sa.Column(sa.Float, nullable=False)
    profit_margin = sa.Column(sa.Float, nullable=False)
    status = sa.Column(sa.String(20), default="DETECTED")  # DETECTED, EXECUTING, COMPLETED, FAILED, PENDING_TG
    
    def __repr__(self):
        return f"<ArbitrageOpportunity(pair='{self.pair}', profit_margin={self.profit_margin}, status='{self.status}')>"


class Trade(Base):
    """Model for storing executed trades."""
    __tablename__ = "trades"
    
    id = sa.Column(sa.Integer, primary_key=True)
    opportunity_id = sa.Column(sa.Integer, sa.ForeignKey("arbitrage_opportunities.id"))
    timestamp = sa.Column(sa.DateTime, default=datetime.utcnow)
    exchange = sa.Column(sa.String(50), nullable=False)
    pair = sa.Column(sa.String(20), nullable=False)
    side = sa.Column(sa.String(10), nullable=False)  # BUY or SELL
    price = sa.Column(sa.Float, nullable=False)
    amount = sa.Column(sa.Float, nullable=False)
    fee = sa.Column(sa.Float, nullable=False)
    order_id = sa.Column(sa.String(100), nullable=False)
    status = sa.Column(sa.String(20), default="OPEN")  # OPEN, FILLED, PARTIALLY_FILLED, CANCELED, FAILED
    
    def __repr__(self):
        return f"<Trade(exchange='{self.exchange}', pair='{self.pair}', side='{self.side}', status='{self.status}')>"


class FundTransfer(Base):
    """Model for storing fund transfers between exchanges."""
    __tablename__ = "fund_transfers"
    
    id = sa.Column(sa.Integer, primary_key=True)
    timestamp = sa.Column(sa.DateTime, default=datetime.utcnow)
    from_exchange = sa.Column(sa.String(50), nullable=False)
    to_exchange = sa.Column(sa.String(50), nullable=False)
    currency = sa.Column(sa.String(10), nullable=False)
    amount = sa.Column(sa.Float, nullable=False)
    fee = sa.Column(sa.Float, nullable=False)
    transaction_id = sa.Column(sa.String(100), nullable=True)
    status = sa.Column(sa.String(20), default="PENDING")  # PENDING, COMPLETED, FAILED
    
    def __repr__(self):
        return f"<FundTransfer(from='{self.from_exchange}', to='{self.to_exchange}', amount={self.amount}, status='{self.status}')>"


class SystemMetric(Base):
    """Model for storing system performance metrics."""
    __tablename__ = "system_metrics"
    
    id = sa.Column(sa.Integer, primary_key=True)
    timestamp = sa.Column(sa.DateTime, default=datetime.utcnow)
    service = sa.Column(sa.String(50), nullable=False)
    metric_name = sa.Column(sa.String(50), nullable=False)
    metric_value = sa.Column(sa.Float, nullable=False)
    
    def __repr__(self):
        return f"<SystemMetric(service='{self.service}', metric_name='{self.metric_name}', value={self.metric_value})>"


# Database utility functions
async def init_db():
    """Initialize database and create tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session():
    """Get database session."""
    async with async_session() as session:
        yield session


async def add_arbitrage_opportunity(session, pair, buy_exchange, sell_exchange, buy_price, sell_price, volume, profit_margin):
    """Add a new arbitrage opportunity to the database."""
    opportunity = ArbitrageOpportunity(
        pair=pair,
        buy_exchange=buy_exchange,
        sell_exchange=sell_exchange,
        buy_price=buy_price,
        sell_price=sell_price,
        volume=volume,
        profit_margin=profit_margin
    )
    session.add(opportunity)
    await session.commit()
    await session.refresh(opportunity)
    return opportunity


async def update_opportunity_status(session, opportunity_id, status):
    """Update the status of an arbitrage opportunity."""
    opportunity = await session.get(ArbitrageOpportunity, opportunity_id)
    if opportunity:
        opportunity.status = status
        await session.commit()
        return True
    return False


async def add_trade(session, opportunity_id, exchange, pair, side, price, amount, fee, order_id):
    """Add a new trade to the database."""
    trade = Trade(
        opportunity_id=opportunity_id,
        exchange=exchange,
        pair=pair,
        side=side,
        price=price,
        amount=amount,
        fee=fee,
        order_id=order_id
    )
    session.add(trade)
    await session.commit()
    await session.refresh(trade)
    return trade


async def update_trade_status(session, trade_id, status):
    """Update the status of a trade."""
    trade = await session.get(Trade, trade_id)
    if trade:
        trade.status = status
        await session.commit()
        return True
    return False


async def add_fund_transfer(session, from_exchange, to_exchange, currency, amount, fee):
    """Add a new fund transfer to the database."""
    transfer = FundTransfer(
        from_exchange=from_exchange,
        to_exchange=to_exchange,
        currency=currency,
        amount=amount,
        fee=fee
    )
    session.add(transfer)
    await session.commit()
    await session.refresh(transfer)
    return transfer


async def update_transfer_status(session, transfer_id, status, transaction_id=None):
    """Update the status of a fund transfer."""
    transfer = await session.get(FundTransfer, transfer_id)
    if transfer:
        transfer.status = status
        if transaction_id:
            transfer.transaction_id = transaction_id
        await session.commit()
        return True
    return False


async def add_system_metric(session, service, metric_name, metric_value):
    """Add a new system metric to the database."""
    metric = SystemMetric(
        service=service,
        metric_name=metric_name,
        metric_value=metric_value
    )
    session.add(metric)
    await session.commit()
    return metric


async def get_recent_opportunities(session, limit=100):
    """Get recent arbitrage opportunities."""
    result = await session.execute(
        select(ArbitrageOpportunity)
        .order_by(ArbitrageOpportunity.timestamp.desc())
        .limit(limit)
    )
    return result.scalars().all()


async def get_daily_pnl(session, date=None):
    """Calculate daily PnL from completed trades."""
    if date is None:
        date = datetime.utcnow().date()
    
    start_date = datetime.combine(date, datetime.min.time())
    end_date = datetime.combine(date, datetime.max.time())
    
    # Get all completed opportunities for the day
    result = await session.execute(
        select(ArbitrageOpportunity)
        .where(
            ArbitrageOpportunity.status == "COMPLETED",
            ArbitrageOpportunity.timestamp >= start_date,
            ArbitrageOpportunity.timestamp <= end_date
        )
    )
    opportunities = result.scalars().all()
    
    total_profit = 0.0
    for opp in opportunities:
        # Calculate actual profit from the trades
        buy_trades_result = await session.execute(
            select(Trade)
            .where(
                Trade.opportunity_id == opp.id,
                Trade.side == "BUY",
                Trade.status == "FILLED"
            )
        )
        buy_trades = buy_trades_result.scalars().all()
        
        sell_trades_result = await session.execute(
            select(Trade)
            .where(
                Trade.opportunity_id == opp.id,
                Trade.side == "SELL",
                Trade.status == "FILLED"
            )
        )
        sell_trades = sell_trades_result.scalars().all()
        
        buy_value = sum(trade.price * trade.amount for trade in buy_trades)
        buy_fees = sum(trade.fee for trade in buy_trades)
        
        sell_value = sum(trade.price * trade.amount for trade in sell_trades)
        sell_fees = sum(trade.fee for trade in sell_trades)
        
        # Add transfer fees
        transfer_result = await session.execute(
            select(FundTransfer)
            .where(
                FundTransfer.timestamp >= opp.timestamp,
                FundTransfer.timestamp <= opp.timestamp + sa.text("interval '1 hour'"),
                FundTransfer.status == "COMPLETED"
            )
        )
        transfers = transfer_result.scalars().all()
        transfer_fees = sum(transfer.fee for transfer in transfers)
        
        # Calculate net profit
        profit = sell_value - buy_value - buy_fees - sell_fees - transfer_fees
        total_profit += profit
    
    return total_profit
