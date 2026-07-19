from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.domain.models import Quote, utc_now


def normalize_order_book(
    *,
    exchange: str,
    symbol: str,
    order_book: dict[str, Any],
    received_at: datetime | None = None,
    buy_volume: float | None = None,
    sell_volume: float | None = None,
    trade_flow_window_seconds: float | None = None,
) -> Quote:
    bids = order_book.get("bids") or []
    asks = order_book.get("asks") or []
    if not bids or not asks:
        raise ValueError("order book must contain at least one bid and ask")

    timestamp_ms = order_book.get("timestamp")
    occurred_at = (
        datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
        if timestamp_ms
        else received_at or utc_now()
    )
    return Quote(
        exchange=exchange,
        symbol=symbol,
        bid=float(bids[0][0]),
        ask=float(asks[0][0]),
        occurred_at=occurred_at,
        received_at=received_at or utc_now(),
        bid_size=float(bids[0][1]) if len(bids[0]) > 1 else None,
        ask_size=float(asks[0][1]) if len(asks[0]) > 1 else None,
        buy_volume=buy_volume,
        sell_volume=sell_volume,
        trade_flow_window_seconds=trade_flow_window_seconds,
    )
