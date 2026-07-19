from app.domain.models import SignalAction, StrategyContext
from app.strategies.base import StrategyConfig
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
