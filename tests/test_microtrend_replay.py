from datetime import UTC, datetime, timedelta

from app.research.live_store import StoredFrame, StoredQuote
from app.research.replay_microtrend import (
    MicroTrendReplayParameters,
    build_parameter_grid,
    replay_symbol_exchange,
)

BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def replay_frame(seconds: int, mid: float) -> StoredFrame:
    sampled_at = BASE_TIME + timedelta(seconds=seconds)
    quote = StoredQuote(
        symbol="TEST/USDT",
        exchange="bybit",
        bid=mid - 0.01,
        ask=mid + 0.01,
        occurred_at=sampled_at,
        received_at=sampled_at,
    )
    return StoredFrame(
        sampled_at_ms=int(sampled_at.timestamp() * 1_000),
        quotes={"bybit": quote},
    )


def test_replay_uses_bid_ask_costs_and_closes_the_final_position() -> None:
    frames = [
        replay_frame(seconds, mid)
        for seconds, mid in zip(
            (0, 30, 60, 90, 120, 150, 180),
            (100, 100, 100, 100, 101, 102, 103),
            strict=True,
        )
    ]
    parameters = MicroTrendReplayParameters(
        fast_seconds=30,
        slow_seconds=90,
        entry_bps=5,
        exit_bps=0,
        stop_loss_bps=1_000,
        take_profit_bps=1_000,
        max_hold_seconds=900,
        cooldown_seconds=0,
        max_quote_age_seconds=3,
    )

    run = replay_symbol_exchange(
        session_id="session",
        symbol="TEST/USDT",
        exchange="bybit",
        frames=frames,
        parameters=parameters,
        fee_bps={"bybit": 10},
        slippage_bps=2,
        notional=100,
        min_trades=1,
        min_profit_factor=1.2,
    )

    assert run.metrics.trade_count == 1
    assert run.metrics.total_pnl_bps > 0
    assert run.metrics.candidate
    assert run.trades[0].fees > 0
    assert run.trades[0].close_reason == "replay end of data"


def test_parameter_grid_discards_fast_windows_above_slow_windows() -> None:
    grid = build_parameter_grid(
        fast_seconds=(30, 300),
        slow_seconds=(180,),
        entry_bps=(5, 10),
        exit_bps=0,
        stop_loss_bps=35,
        take_profit_bps=70,
        max_hold_seconds=900,
        cooldown_seconds=20,
        max_quote_age_seconds=3,
    )

    assert [(item.fast_seconds, item.slow_seconds) for item in grid] == [
        (30, 180),
        (30, 180),
    ]
