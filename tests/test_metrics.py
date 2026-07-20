import pytest

from app.analytics.metrics import calculate_metrics
from tests.helpers import closed_trade


def test_metrics_cover_core_paper_statistics() -> None:
    metrics = calculate_metrics(
        [
            closed_trade(trade_id="1", pnl=10, hold_seconds=30),
            closed_trade(trade_id="2", pnl=-5, hold_seconds=90),
            closed_trade(trade_id="3", pnl=15, hold_seconds=60),
        ],
        initial_equity=100,
    )

    assert metrics.pnl == 20
    assert metrics.trade_count == 3
    assert metrics.win_rate == pytest.approx(2 / 3)
    assert metrics.profit_factor == pytest.approx(5)
    assert metrics.max_drawdown == 5
    assert metrics.average_hold_seconds == 60
    assert metrics.expectancy == pytest.approx(20 / 3)
    assert metrics.average_profit == 12.5
    assert metrics.average_loss == -5

