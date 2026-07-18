from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from app.domain.models import Quote, utc_now
from app.market_data.normalizer import normalize_order_book


class RestMarketDataFeed:
    def __init__(
        self,
        *,
        exchanges: tuple[str, ...],
        symbols: tuple[str, ...],
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self.exchange_ids = exchanges
        self.symbols = symbols
        self.poll_interval_seconds = poll_interval_seconds
        self._clients: list = []

    async def quotes(self) -> AsyncIterator[Quote]:
        try:
            import ccxt.async_support as ccxt
        except ImportError as error:
            raise RuntimeError("install ccxt to use MARKET_DATA_MODE=rest") from error

        try:
            for exchange_id in self.exchange_ids:
                exchange_class = getattr(ccxt, exchange_id)
                client = exchange_class(
                    {"enableRateLimit": True, "options": {"defaultType": "spot"}}
                )
                await client.load_markets()
                self._clients.append(client)

            while True:
                for client in self._clients:
                    for symbol in self.symbols:
                        if symbol not in client.markets:
                            continue
                        order_book = await client.fetch_order_book(symbol, limit=5)
                        yield normalize_order_book(
                            exchange=client.id,
                            symbol=symbol,
                            order_book=order_book,
                            received_at=utc_now(),
                        )
                await asyncio.sleep(self.poll_interval_seconds)
        finally:
            await self.close()

    async def close(self) -> None:
        for client in self._clients:
            await client.close()
