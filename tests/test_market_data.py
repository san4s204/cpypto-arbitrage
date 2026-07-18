import asyncio

import pytest

from app.market_data.ws_listener import CcxtProMarketDataFeed


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
