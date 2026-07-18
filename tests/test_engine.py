from pathlib import Path

import pytest

from app.analytics.storage import TradeStore
from app.bot.telegram import NullNotifier
from app.engine import TradingEngine
from app.risk.position_sizer import PositionSizer, PositionSizerConfig
from app.risk.risk_engine import RiskConfig, RiskEngine
from app.strategies.base import StrategyConfig
from app.strategies.spread_reaction import SpreadReactionStrategy
from app.trading.paper_trader import PaperTrader
from tests.helpers import quote


def build_engine(db_path: Path) -> TradingEngine:
    strategy = SpreadReactionStrategy(
        StrategyConfig(
            strategy_id="spread",
            symbols=("BTC/USDT",),
            exchanges=("bybit", "okx"),
            parameters={"entry_bps": 20, "exit_bps": 15, "max_quote_age_seconds": 5},
        )
    )
    return TradingEngine(
        strategies=[strategy],
        risk_engine=RiskEngine(
            RiskConfig(
                max_open_positions=2,
                max_notional_per_position=100,
                max_total_exposure=200,
                max_daily_loss=50,
                max_quote_age_seconds=5,
                min_signal_confidence=0,
                cooldown_seconds=0,
            )
        ),
        position_sizer=PositionSizer(
            PositionSizerConfig(
                allocation_fraction=0.03,
                min_notional=50,
                max_notional=100,
            )
        ),
        paper_trader=PaperTrader(
            initial_equity=3_000,
            fee_bps={"bybit": 0, "okx": 0},
            slippage_bps=0,
        ),
        trade_store=TradeStore(db_path),
        notifier=NullNotifier(),
    )


@pytest.mark.asyncio
async def test_engine_opens_and_closes_spread_legs_atomically(tmp_path: Path) -> None:
    engine = build_engine(tmp_path / "trades.sqlite3")

    await engine.handle_quote(quote("bybit", bid=99.9, ask=100))
    await engine.handle_quote(quote("okx", bid=101, ask=101.1))
    assert len(engine.paper_trader.positions) == 2

    await engine.handle_quote(quote("bybit", bid=100.4, ask=100.5, seconds=1))
    await engine.handle_quote(quote("okx", bid=100.6, ask=100.7, seconds=1))

    assert engine.paper_trader.positions == {}
    assert len(engine.paper_trader.closed_trades) == 2
    assert engine.metrics.trade_count == 2
