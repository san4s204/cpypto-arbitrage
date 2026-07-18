import pytest

from app.domain.models import Signal, SignalAction
from app.trading.paper_trader import PaperTrader
from tests.helpers import quote


def test_long_trade_includes_entry_and_exit_fees() -> None:
    trader = PaperTrader(
        initial_equity=3_000,
        fee_bps={"bybit": 10},
        slippage_bps=0,
    )
    entry_quote = quote("bybit", bid=100, ask=101)
    trader.handle_signal(
        Signal(
            strategy_id="micro",
            action=SignalAction.ENTER_LONG,
            exchange="bybit",
            symbol="BTC/USDT",
            reason="entry",
            created_at=entry_quote.received_at,
        ),
        entry_quote,
        notional=1_000,
    )

    exit_quote = quote("bybit", bid=110, ask=111, seconds=60)
    trade = trader.handle_signal(
        Signal(
            strategy_id="micro",
            action=SignalAction.EXIT_LONG,
            exchange="bybit",
            symbol="BTC/USDT",
            reason="exit",
            created_at=exit_quote.received_at,
        ),
        exit_quote,
    )

    assert trade is not None
    quantity = 1_000 / 101
    expected_gross = (110 - 101) * quantity
    expected_fees = 1 + (110 * quantity * 0.001)
    assert trade.gross_pnl == pytest.approx(expected_gross)
    assert trade.fees == pytest.approx(expected_fees)
    assert trade.net_pnl == pytest.approx(expected_gross - expected_fees)
    assert trader.equity == pytest.approx(3_000 + trade.net_pnl)


def test_short_trade_profits_when_price_falls() -> None:
    trader = PaperTrader(
        initial_equity=3_000,
        fee_bps={"okx": 0},
        slippage_bps=0,
    )
    entry_quote = quote("okx", bid=100, ask=101)
    trader.handle_signal(
        Signal(
            strategy_id="latency",
            action=SignalAction.ENTER_SHORT,
            exchange="okx",
            symbol="BTC/USDT",
            reason="entry",
            created_at=entry_quote.received_at,
        ),
        entry_quote,
        notional=500,
    )
    exit_quote = quote("okx", bid=89, ask=90, seconds=30)
    trade = trader.handle_signal(
        Signal(
            strategy_id="latency",
            action=SignalAction.EXIT_SHORT,
            exchange="okx",
            symbol="BTC/USDT",
            reason="exit",
            created_at=exit_quote.received_at,
        ),
        exit_quote,
    )

    assert trade is not None
    assert trade.net_pnl == pytest.approx(50)


def test_mark_quote_applies_paper_stop_loss() -> None:
    trader = PaperTrader(
        initial_equity=3_000,
        fee_bps={"bybit": 0},
        slippage_bps=0,
    )
    entry_quote = quote("bybit", bid=100, ask=100)
    trader.handle_signal(
        Signal(
            strategy_id="micro",
            action=SignalAction.ENTER_LONG,
            exchange="bybit",
            symbol="BTC/USDT",
            reason="entry",
            stop_loss_bps=50,
            created_at=entry_quote.received_at,
        ),
        entry_quote,
        notional=100,
    )

    trades = trader.mark_quote(quote("bybit", bid=99, ask=99.1, seconds=5))

    assert len(trades) == 1
    assert trades[0].close_reason == "paper stop loss"

