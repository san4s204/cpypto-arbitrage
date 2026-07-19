from datetime import UTC, datetime, timedelta

from app.domain.models import ClosedTrade, PositionSide, Quote

BASE_TIME = datetime.now(UTC)


def quote(
    exchange: str,
    *,
    bid: float,
    ask: float,
    symbol: str = "BTC/USDT",
    seconds: float = 0,
    bid_size: float | None = None,
    ask_size: float | None = None,
    buy_volume: float | None = None,
    sell_volume: float | None = None,
    trade_flow_window_seconds: float | None = None,
) -> Quote:
    timestamp = BASE_TIME + timedelta(seconds=seconds)
    return Quote(
        exchange=exchange,
        symbol=symbol,
        bid=bid,
        ask=ask,
        occurred_at=timestamp,
        received_at=timestamp,
        bid_size=bid_size,
        ask_size=ask_size,
        buy_volume=buy_volume,
        sell_volume=sell_volume,
        trade_flow_window_seconds=trade_flow_window_seconds,
    )


def closed_trade(
    *,
    trade_id: str,
    pnl: float,
    hold_seconds: float = 60,
    notional: float = 100,
) -> ClosedTrade:
    opened_at = BASE_TIME
    return ClosedTrade(
        trade_id=trade_id,
        strategy_id="test",
        exchange="bybit",
        symbol="BTC/USDT",
        side=PositionSide.LONG,
        quantity=1,
        entry_price=100,
        exit_price=100 + pnl,
        entry_notional=notional,
        gross_pnl=pnl,
        fees=0,
        net_pnl=pnl,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(seconds=hold_seconds),
        close_reason="test",
    )
