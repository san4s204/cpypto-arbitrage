from datetime import UTC, datetime, timedelta

from app.research.live_store import StoredFrame, StoredQuote
from app.research.replay_latency import (
    LatencyReplayParameters,
    build_parameter_grid,
    replay_latency_symbol,
)

BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def latency_frame(seconds: int, *, bybit: float, okx: float) -> StoredFrame:
    sampled_at = BASE_TIME + timedelta(seconds=seconds)
    return StoredFrame(
        sampled_at_ms=int(sampled_at.timestamp() * 1_000),
        quotes={
            exchange: StoredQuote(
                symbol="TEST/USDT",
                exchange=exchange,
                bid=mid - 0.01,
                ask=mid + 0.01,
                occurred_at=sampled_at,
                received_at=sampled_at,
            )
            for exchange, mid in {"bybit": bybit, "okx": okx}.items()
        },
    )


def parameters(*, response_bps: float) -> LatencyReplayParameters:
    return LatencyReplayParameters(
        lookback_seconds=5,
        leader_move_bps=50,
        min_gap_bps=40,
        response_bps=response_bps,
        stop_move_bps=30,
        max_hold_seconds=10,
        cooldown_seconds=0,
        max_quote_age_seconds=3,
    )


def test_latency_replay_executes_the_follower_at_bid_ask_after_costs() -> None:
    frames = [
        latency_frame(0, bybit=100, okx=100),
        latency_frame(5, bybit=101, okx=100),
        latency_frame(10, bybit=101, okx=100.5),
    ]

    run = replay_latency_symbol(
        session_id="session",
        symbol="TEST/USDT",
        exchanges=("bybit", "okx"),
        frames=frames,
        parameters=parameters(response_bps=40),
        fee_bps={"bybit": 10, "okx": 10},
        slippage_bps=2,
        notional=100,
        min_trades=1,
        min_profit_factor=1.2,
    )

    assert run.metrics.trade_count == 1
    assert run.metrics.expectancy_bps > 0
    assert run.metrics.candidate
    assert run.trades[0].exchange == "okx"
    assert run.trades[0].fees > 0
    assert run.trades[0].close_reason == "follower response target"


def test_latency_replay_rejects_a_response_that_does_not_cover_costs() -> None:
    frames = [
        latency_frame(0, bybit=100, okx=100),
        latency_frame(5, bybit=101, okx=100),
        latency_frame(10, bybit=101, okx=100.1),
    ]

    run = replay_latency_symbol(
        session_id="session",
        symbol="TEST/USDT",
        exchanges=("bybit", "okx"),
        frames=frames,
        parameters=parameters(response_bps=8),
        fee_bps={"bybit": 10, "okx": 10},
        slippage_bps=2,
        notional=100,
        min_trades=1,
        min_profit_factor=1.2,
    )

    assert run.metrics.trade_count == 1
    assert run.metrics.expectancy_bps < 0
    assert not run.metrics.candidate


def test_latency_parameter_grid_deduplicates_values() -> None:
    grid = build_parameter_grid(
        lookback_seconds=(5, 5),
        leader_move_bps=(12, 20),
        min_gap_bps=(8,),
        response_bps=(12, 20),
        stop_move_bps=30,
        max_hold_seconds=(5, 10),
        cooldown_seconds=5,
        max_quote_age_seconds=3,
    )

    assert len(grid) == 8
