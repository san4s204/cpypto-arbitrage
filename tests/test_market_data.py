import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.market_data.normalizer import normalize_order_book
from app.market_data.ws_listener import CcxtProMarketDataFeed, RollingTradeFlow


class RecordingClient:
    def __init__(self) -> None:
        self.requested_limit: int | None = None

    async def watch_order_book(self, symbol: str, *, limit: int) -> dict:
        self.requested_limit = limit
        raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_websocket_feed_requests_exchange_compatible_top_of_book() -> None:
    feed = CcxtProMarketDataFeed(
        exchanges=("bybit",),
        symbols=("ETH/USDT",),
    )
    client = RecordingClient()

    with pytest.raises(asyncio.CancelledError):
        await feed._watch(client, "bybit", "ETH/USDT")

    assert client.requested_limit == 1


def test_order_book_normalizer_keeps_sizes_and_trade_flow() -> None:
    timestamp = datetime.now(UTC)
    market_quote = normalize_order_book(
        exchange="bybit",
        symbol="ETH/USDT",
        order_book={
            "bids": [[100, 8]],
            "asks": [[101, 2]],
            "timestamp": int(timestamp.timestamp() * 1_000),
        },
        received_at=timestamp,
        buy_volume=750,
        sell_volume=250,
        trade_flow_window_seconds=60,
    )

    assert market_quote.bid_size == 8
    assert market_quote.ask_size == 2
    assert market_quote.book_imbalance == pytest.approx(0.6)
    assert market_quote.trade_flow_imbalance == pytest.approx(0.5)


def test_rolling_trade_flow_deduplicates_and_expires_trades() -> None:
    timestamp = datetime.now(UTC)
    flow = RollingTradeFlow(window_seconds=60)
    trades = [
        {
            "id": "buy",
            "timestamp": int(timestamp.timestamp() * 1_000),
            "side": "buy",
            "amount": 2,
            "price": 100,
        },
        {
            "id": "sell",
            "timestamp": int(timestamp.timestamp() * 1_000),
            "side": "sell",
            "cost": 50,
        },
    ]

    flow.update(trades, timestamp)
    flow.update(trades, timestamp)

    assert flow.snapshot(timestamp) == (200, 50)
    assert flow.snapshot(timestamp + timedelta(seconds=61)) == (0, 0)
