from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.domain.models import Quote, utc_now
from app.market_data.normalizer import normalize_order_book

logger = logging.getLogger(__name__)
TOP_OF_BOOK_LIMIT = 1


def _client_config() -> dict[str, Any]:
    return {
        "enableRateLimit": True,
        "newUpdates": True,
        "options": {"defaultType": "spot"},
    }


@dataclass(slots=True)
class RollingTradeFlow:
    window_seconds: float
    max_seen: int = 10_000
    events: deque[tuple[int, str, float]] = field(default_factory=deque)
    seen_order: deque[str] = field(default_factory=deque)
    seen: set[str] = field(default_factory=set)

    def _prune(self, now: datetime) -> None:
        cutoff_ms = int((now.timestamp() - self.window_seconds) * 1_000)
        while self.events and self.events[0][0] < cutoff_ms:
            self.events.popleft()

    def update(self, trades: list[dict[str, Any]], received_at: datetime) -> None:
        cutoff_ms = int((received_at.timestamp() - self.window_seconds) * 1_000)
        ordered_trades = sorted(
            trades,
            key=lambda trade: int(trade.get("timestamp") or 0),
        )
        for trade in ordered_trades:
            side = str(trade.get("side") or "").lower()
            if side not in {"buy", "sell"}:
                continue
            timestamp_ms = int(trade.get("timestamp") or received_at.timestamp() * 1_000)
            if timestamp_ms < cutoff_ms:
                continue
            amount = float(trade.get("amount") or 0)
            price = float(trade.get("price") or 0)
            notional = float(trade.get("cost") or amount * price)
            if notional <= 0:
                continue
            trade_id = str(
                trade.get("id")
                or f"{timestamp_ms}:{side}:{price:.12g}:{amount:.12g}"
            )
            if trade_id in self.seen:
                continue
            if len(self.seen_order) >= self.max_seen:
                self.seen.discard(self.seen_order.popleft())
            self.seen.add(trade_id)
            self.seen_order.append(trade_id)
            self.events.append((timestamp_ms, side, notional))
        self._prune(received_at)

    def snapshot(self, now: datetime) -> tuple[float, float]:
        self._prune(now)
        buy_volume = sum(value for _, side, value in self.events if side == "buy")
        sell_volume = sum(value for _, side, value in self.events if side == "sell")
        return buy_volume, sell_volume


class CcxtProMarketDataFeed:
    def __init__(
        self,
        *,
        exchanges: tuple[str, ...],
        symbols: tuple[str, ...],
        reconnect_delay_seconds: float = 2.0,
        trade_flow_window_seconds: float = 60.0,
    ) -> None:
        if trade_flow_window_seconds <= 0:
            raise ValueError("trade_flow_window_seconds must be positive")
        self.exchange_ids = exchanges
        self.symbols = symbols
        self.reconnect_delay_seconds = reconnect_delay_seconds
        self.trade_flow_window_seconds = trade_flow_window_seconds
        self._queue: asyncio.Queue[Quote] = asyncio.Queue(maxsize=10_000)
        self._clients: list = []
        self._tasks: list[asyncio.Task] = []
        self._trade_flows: dict[tuple[str, str], RollingTradeFlow] = defaultdict(
            lambda: RollingTradeFlow(self.trade_flow_window_seconds)
        )

    async def _watch(self, client, exchange_id: str, symbol: str) -> None:
        while True:
            try:
                order_book = await client.watch_order_book(
                    symbol,
                    limit=TOP_OF_BOOK_LIMIT,
                    params={},
                )
                await self._publish_order_book(exchange_id, symbol, order_book)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("%s %s market data error: %s", exchange_id, symbol, error)
                await asyncio.sleep(self.reconnect_delay_seconds)

    async def _publish_order_book(
        self,
        exchange_id: str,
        symbol: str,
        order_book: dict[str, Any],
    ) -> None:
        received_at = utc_now()
        buy_volume, sell_volume = self._trade_flows[
            (exchange_id, symbol)
        ].snapshot(received_at)
        quote = normalize_order_book(
            exchange=exchange_id,
            symbol=symbol,
            order_book=order_book,
            received_at=received_at,
            buy_volume=buy_volume,
            sell_volume=sell_volume,
            trade_flow_window_seconds=self.trade_flow_window_seconds,
        )
        await self._queue.put(quote)

    async def _watch_trades(self, client, exchange_id: str, symbol: str) -> None:
        while True:
            try:
                trades = await client.watch_trades(symbol)
                self._trade_flows[(exchange_id, symbol)].update(trades, utc_now())
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "%s %s trade flow error: %s",
                    exchange_id,
                    symbol,
                    error,
                )
                await asyncio.sleep(self.reconnect_delay_seconds)

    async def quotes(self) -> AsyncIterator[Quote]:
        try:
            import ccxt.pro as ccxtpro
        except ImportError as error:
            raise RuntimeError("install ccxt to use MARKET_DATA_MODE=ws") from error

        try:
            for exchange_id in self.exchange_ids:
                exchange_class = getattr(ccxtpro, exchange_id)
                client = exchange_class(_client_config())
                self._clients.append(client)
                await client.load_markets()
                for symbol in self.symbols:
                    if symbol not in client.markets:
                        logger.warning("%s does not list %s", exchange_id, symbol)
                        continue
                    if client.markets[symbol].get("active") is False:
                        logger.warning("%s lists %s as inactive", exchange_id, symbol)
                        continue
                    self._tasks.append(
                        asyncio.create_task(self._watch(client, exchange_id, symbol))
                    )
                    self._tasks.append(
                        asyncio.create_task(
                            self._watch_trades(client, exchange_id, symbol)
                        )
                    )

            if not self._tasks:
                raise RuntimeError("no market data subscriptions were created")
            while True:
                yield await self._queue.get()
        finally:
            await self.close()

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        for client in self._clients:
            await client.close()
