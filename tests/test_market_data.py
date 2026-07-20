import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.market_data.normalizer import normalize_order_book
from app.market_data.ws_listener import (
    CcxtProMarketDataFeed,
    RollingTradeFlow,
    _configure_websocket_client,
)


class RecordingClient:
    def __init__(self) -> None:
        self.requested_limit: int | None = None
        self.requested_params: dict | None = None

    async def watch_order_book(self, symbol: str, *, limit: int, params: dict) -> dict:
        self.requested_limit = limit
        self.requested_params = params
        raise asyncio.CancelledError


def test_mexc_client_uses_the_documented_uppercase_ping() -> None:
    client = RecordingClient()
    client.ping = lambda _: {"method": "ping"}

    configured = _configure_websocket_client(client, "mexc")

    assert configured is client
    assert client.ping(None) == {"method": "PING"}


def test_other_exchange_ping_is_not_modified() -> None:
    client = RecordingClient()

    def original_ping(_client) -> dict[str, str]:
        return {"op": "ping"}

    client.ping = original_ping

    _configure_websocket_client(client, "bybit")

    assert client.ping is original_ping


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
    assert client.requested_params == {}


@pytest.mark.asyncio
async def test_mexc_websocket_feed_requests_100ms_updates() -> None:
    feed = CcxtProMarketDataFeed(
        exchanges=("mexc",),
        symbols=("ETH/USDT",),
    )
    client = RecordingClient()

    with pytest.raises(asyncio.CancelledError):
        await feed._watch(client, "mexc", "ETH/USDT")

    assert client.requested_limit == 1
    assert client.requested_params == {"frequency": "100ms"}


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
