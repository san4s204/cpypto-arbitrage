from app.domain.models import Signal, SignalAction
from app.risk.position_sizer import PositionSizer, PositionSizerConfig
from app.risk.risk_engine import RiskConfig, RiskEngine
from tests.helpers import quote


def test_position_sizer_never_allocates_more_than_equity() -> None:
    sizer = PositionSizer(
        PositionSizerConfig(
            allocation_fraction=0.5,
            min_notional=10,
            max_notional=100,
        )
    )

    assert sizer.size(5) == 5
    assert sizer.size(0) == 0


def test_risk_engine_rejects_stale_market_data() -> None:
    market_quote = quote("bybit", bid=100, ask=101, seconds=-30)
    signal = Signal(
        strategy_id="test",
        action=SignalAction.ENTER_LONG,
        exchange="bybit",
        symbol="BTC/USDT",
        reason="test",
        confidence=1,
    )
    decision = RiskEngine(RiskConfig(max_quote_age_seconds=2)).assess(
        signal,
        market_quote,
        notional=100,
        open_positions=0,
        total_exposure=0,
        daily_realized_pnl=0,
    )

    assert not decision.allowed
    assert decision.reason == "stale quote"
