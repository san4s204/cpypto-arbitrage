import pytest

from app.domain.models import SignalAction, StrategyContext
from app.strategies.base import StrategyConfig
from app.strategies.confirmed_impulse import ConfirmedImpulseStrategy
from app.strategies.micro_trend import MicroTrendStrategy
from app.strategies.spread_reaction import SpreadReactionStrategy
from tests.helpers import quote


def test_micro_trend_emits_long_entry() -> None:
    strategy = MicroTrendStrategy(
        StrategyConfig(
            strategy_id="micro",
            symbols=("BTC/USDT",),
            exchanges=("bybit",),
            parameters={"fast_window": 1, "slow_window": 3, "entry_bps": 5},
        )
    )

    assert strategy.on_quote(
        quote("bybit", bid=99.9, ask=100.1), StrategyContext()
    ) == []
    assert strategy.on_quote(
        quote("bybit", bid=99.9, ask=100.1, seconds=1), StrategyContext()
    ) == []
    signals = strategy.on_quote(
        quote("bybit", bid=100.9, ask=101.1, seconds=2), StrategyContext()
    )

    assert len(signals) == 1
    assert signals[0].action is SignalAction.ENTER_LONG


def test_micro_trend_uses_elapsed_time_instead_of_quote_count() -> None:
    strategy = MicroTrendStrategy(
        StrategyConfig(
            strategy_id="micro_time",
            symbols=("BTC/USDT",),
            exchanges=("bybit",),
            parameters={"fast_seconds": 30, "slow_seconds": 60, "entry_bps": 5},
        )
    )

    for seconds in (0, 20, 40, 59):
        assert strategy.on_quote(
            quote("bybit", bid=99.9, ask=100.1, seconds=seconds),
            StrategyContext(),
        ) == []

    market_quote = quote("bybit", bid=100.9, ask=101.1, seconds=60)
    signals = strategy.on_quote(market_quote, StrategyContext())

    assert len(signals) == 1
    assert signals[0].action is SignalAction.ENTER_LONG
    assert signals[0].created_at == market_quote.received_at


def test_spread_reaction_emits_atomic_pair() -> None:
    strategy = SpreadReactionStrategy(
        StrategyConfig(
            strategy_id="spread",
            symbols=("BTC/USDT",),
            exchanges=("bybit", "okx"),
            parameters={"entry_bps": 20, "exit_bps": 5},
        )
    )

    assert strategy.on_quote(
        quote("bybit", bid=99.9, ask=100), StrategyContext()
    ) == []
    signals = strategy.on_quote(
        quote("okx", bid=101, ask=101.1), StrategyContext()
    )

    assert {signal.action for signal in signals} == {
        SignalAction.ENTER_LONG,
        SignalAction.ENTER_SHORT,
    }
    assert len({signal.metadata["group_id"] for signal in signals}) == 1


def test_confirmed_impulse_requires_both_exchanges_and_feature_confirmation() -> None:
    strategy = ConfirmedImpulseStrategy(
        StrategyConfig(
            strategy_id="confirmed",
            symbols=("BTC/USDT",),
            exchanges=("bybit", "okx"),
            parameters={
                "lookback_seconds": 60,
                "round_trip_cost_bps": 24,
                "safety_margin_bps": 6,
                "min_confirmed_move_bps": 30,
                "max_exchange_divergence_bps": 10,
                "min_book_imbalance": 0.2,
                "min_trade_flow_imbalance": 0.2,
            },
        )
    )

    def feature_quote(exchange: str, mid: float, seconds: int):
        return quote(
            exchange,
            bid=mid - 0.01,
            ask=mid + 0.01,
            seconds=seconds,
            bid_size=80,
            ask_size=20,
            buy_volume=800,
            sell_volume=200,
            trade_flow_window_seconds=60,
        )

    assert strategy.on_quote(feature_quote("bybit", 100, 0), StrategyContext()) == []
    assert strategy.on_quote(feature_quote("okx", 100, 0), StrategyContext()) == []
    assert strategy.on_quote(feature_quote("bybit", 100.4, 60), StrategyContext()) == []

    signals = strategy.on_quote(
        feature_quote("okx", 100.4, 60),
        StrategyContext(),
    )

    assert len(signals) == 1
    assert signals[0].action is SignalAction.ENTER_LONG
    assert signals[0].metadata["confirmed_move_bps"] == pytest.approx(40)
    assert signals[0].metadata["round_trip_cost_bps"] == 24
