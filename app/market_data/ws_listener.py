from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from app.domain.models import Quote, utc_now
from app.market_data.normalizer import normalize_order_book

logger = logging.getLogger(__name__)
TOP_OF_BOOK_LIMIT = 1


class CcxtProMarketDataFeed:
    def __init__(
        self,
        *,
        exchanges: tuple[str, ...],
        symbols: tuple[str, ...],
        reconnect_delay_seconds: float = 2.0,
    ) -> None:
        self.exchange_ids = exchanges
        self.symbols = symbols
        self.reconnect_delay_seconds = reconnect_delay_seconds
        self._queue: asyncio.Queue[Quote] = asyncio.Queue(maxsize=10_000)
        self._clients: list = []
        self._tasks: list[asyncio.Task] = []

    async def _watch(self, client, exchange_id: str, symbol: str) -> None:
        while True:
            try:
                order_book = await client.watch_order_book(
                    symbol,
                    limit=TOP_OF_BOOK_LIMIT,
                )
                quote = normalize_order_book(
                    exchange=exchange_id,
                    symbol=symbol,
                    order_book=order_book,
                    received_at=utc_now(),
                )
                await self._queue.put(quote)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("%s %s market data error: %s", exchange_id, symbol, error)
                await asyncio.sleep(self.reconnect_delay_seconds)

    async def quotes(self) -> AsyncIterator[Quote]:
        try:
            import ccxt.pro as ccxtpro
        except ImportError as error:
            raise RuntimeError("install ccxt to use MARKET_DATA_MODE=ws") from error

        try:
            for exchange_id in self.exchange_ids:
                exchange_class = getattr(ccxtpro, exchange_id)
                client = exchange_class(
                    {
                        "enableRateLimit": True,
                        "options": {"defaultType": "spot"},
                    }
                )
                self._clients.append(client)
                await client.load_markets()
                for symbol in self.symbols:
                    if symbol not in client.markets:
                        logger.warning("%s does not list %s", exchange_id, symbol)
                        continue
                    self._tasks.append(
                        asyncio.create_task(self._watch(client, exchange_id, symbol))
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
