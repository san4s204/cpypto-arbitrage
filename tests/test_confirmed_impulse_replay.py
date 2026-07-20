from datetime import UTC, datetime, timedelta

from app.research.live_store import StoredFrame, StoredQuote
from app.research.replay_confirmed_impulse import (
    ConfirmedImpulseReplayParameters,
    feature_coverage,
    replay_confirmed_impulse_symbol,
)

BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def feature_frame(seconds: int, mid: float) -> StoredFrame:
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
                bid_size=80,
                ask_size=20,
                buy_volume=800,
                sell_volume=200,
                trade_flow_window_seconds=60,
            )
            for exchange in ("bybit", "okx")
        },
    )


def test_confirmed_impulse_replay_executes_a_cost_aware_trade() -> None:
    frames = [
        feature_frame(0, 100),
        feature_frame(30, 100),
        feature_frame(60, 100.4),
        feature_frame(90, 102),
    ]
    parameters = ConfirmedImpulseReplayParameters(
        lookback_seconds=60,
        min_confirmed_move_bps=30,
        max_exchange_divergence_bps=10,
        min_book_imbalance=0.2,
        min_trade_flow_imbalance=0.2,
        round_trip_cost_bps=24,
        safety_margin_bps=6,
        exit_momentum_bps=5,
        exit_book_imbalance=-0.05,
        exit_trade_flow_imbalance=-0.05,
        stop_loss_bps=60,
        take_profit_bps=120,
        max_hold_seconds=300,
        cooldown_seconds=20,
        max_quote_age_seconds=3,
    )

    run = replay_confirmed_impulse_symbol(
        session_id="session",
        symbol="TEST/USDT",
        exchanges=("bybit", "okx"),
        frames=frames,
        parameters=parameters,
        fee_bps={"bybit": 10, "okx": 10},
        slippage_bps=2,
        notional=100,
        min_trades=1,
        min_profit_factor=1.2,
    )

    assert feature_coverage(frames, ("bybit", "okx")) == 1
    assert run.metrics.trade_count == 1
    assert run.metrics.expectancy_bps > 120
    assert run.metrics.candidate
