from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from statistics import mean
from typing import Any

logger = logging.getLogger(__name__)

STABLE_BASES = {
    "BUSD",
    "DAI",
    "FDUSD",
    "FRAX",
    "PYUSD",
    "TUSD",
    "USD1",
    "USDC",
    "USDD",
    "USDE",
    "USDP",
    "USDT",
}
LEVERAGED_SUFFIXES = ("2L", "2S", "3L", "3S", "5L", "5S", "BULL", "BEAR")


@dataclass(frozen=True, slots=True)
class PairLiquidity:
    symbol: str
    exchanges: tuple[str, ...]
    min_quote_volume: float
    min_depth: float


@dataclass(frozen=True, slots=True)
class PairScore:
    symbol: str
    exchanges: tuple[str, ...]
    min_quote_volume: float
    min_depth: float
    candle_count: int
    mean_gross_spread_bps: float
    p95_gross_spread_bps: float
    p95_net_edge_bps: float
    max_gross_spread_bps: float
    opportunity_count: int
    opportunity_rate: float
    mean_forward_pnl_bps: float
    forward_win_rate: float
    score: float
    eligible: bool

    def as_row(self) -> dict[str, str | int | float | bool]:
        row = asdict(self)
        row["exchanges"] = ",".join(self.exchanges)
        return row


@dataclass(frozen=True, slots=True)
class UniverseSelectorConfig:
    exchanges: tuple[str, ...] = ("bybit", "okx", "mexc")
    min_exchanges: int = 2
    days: int = 14
    timeframe: str = "15m"
    max_history_pairs: int = 40
    liquidity_prefilter_multiplier: int = 2
    min_quote_volume: float = 200_000
    min_depth: float = 2_000
    depth_band_bps: float = 10
    min_net_edge_bps: float = 5
    min_opportunities: int = 3
    min_forward_win_rate: float = 0.5
    holding_bars: int = 1
    concurrency: int = 4

    def __post_init__(self) -> None:
        if len(self.exchanges) < 2:
            raise ValueError("universe selection requires at least two exchanges")
        if not 2 <= self.min_exchanges <= len(self.exchanges):
            raise ValueError("min_exchanges must be between two and exchange count")
        for name in (
            "days",
            "max_history_pairs",
            "liquidity_prefilter_multiplier",
            "min_opportunities",
            "holding_bars",
            "concurrency",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not 0 <= self.min_forward_win_rate <= 1:
            raise ValueError("min_forward_win_rate must be between 0 and 1")
        for name in (
            "min_quote_volume",
            "min_depth",
            "depth_band_bps",
            "min_net_edge_bps",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")


def is_candidate_symbol(symbol: str) -> bool:
    if "/" not in symbol or ":" in symbol:
        return False
    base, quote = (part.upper() for part in symbol.split("/", maxsplit=1))
    if quote != "USDT" or base in STABLE_BASES:
        return False
    return not base.endswith(LEVERAGED_SUFFIXES)


def common_spot_symbols(
    markets_by_exchange: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> list[str]:
    coverage = spot_symbol_exchanges(markets_by_exchange)
    exchange_count = len(markets_by_exchange)
    return sorted(
        symbol
        for symbol, exchanges in coverage.items()
        if len(exchanges) == exchange_count
    )


def spot_symbol_exchanges(
    markets_by_exchange: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> dict[str, tuple[str, ...]]:
    coverage: dict[str, list[str]] = {}
    for exchange, markets in markets_by_exchange.items():
        symbols = {
            str(market["symbol"])
            for market in markets.values()
            if market.get("spot")
            and market.get("active") is not False
            and market.get("symbol")
            and is_candidate_symbol(str(market["symbol"]))
        }
        for symbol in symbols:
            coverage.setdefault(symbol, []).append(exchange)
    return {
        symbol: tuple(sorted(exchanges))
        for symbol, exchanges in coverage.items()
    }


def quote_volume(ticker: Mapping[str, Any] | None) -> float:
    if not ticker:
        return 0.0
    direct = ticker.get("quoteVolume")
    if direct is not None:
        return max(0.0, float(direct))
    base_volume = ticker.get("baseVolume")
    last = ticker.get("last")
    if base_volume is None or last is None:
        return 0.0
    return max(0.0, float(base_volume) * float(last))


def depth_within_band(order_book: Mapping[str, Any], band_bps: float) -> float:
    bids = order_book.get("bids") or []
    asks = order_book.get("asks") or []
    if not bids or not asks:
        return 0.0
    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    bid_floor = best_bid * (1 - band_bps / 10_000)
    ask_ceiling = best_ask * (1 + band_bps / 10_000)
    bid_depth = sum(
        float(price) * float(amount)
        for price, amount, *_ in bids
        if float(price) >= bid_floor
    )
    ask_depth = sum(
        float(price) * float(amount)
        for price, amount, *_ in asks
        if float(price) <= ask_ceiling
    )
    return min(bid_depth, ask_depth)


def percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def round_trip_cost_bps(
    buy_exchange: str,
    sell_exchange: str,
    *,
    fee_bps: Mapping[str, float],
    slippage_bps: float,
) -> float:
    entry_and_exit_fees = 2 * (
        float(fee_bps.get(buy_exchange, 0))
        + float(fee_bps.get(sell_exchange, 0))
    )
    four_paper_fills = 4 * slippage_bps
    return entry_and_exit_fees + four_paper_fills


def _candle_mids(candles: Sequence[Sequence[float]]) -> dict[int, float]:
    result: dict[int, float] = {}
    for candle in candles:
        if len(candle) < 4:
            continue
        timestamp = int(candle[0])
        high = float(candle[2])
        low = float(candle[3])
        if high > 0 and low > 0:
            result[timestamp] = (high + low) / 2
    return result


def score_pair_history(
    symbol: str,
    candles_by_exchange: Mapping[str, Sequence[Sequence[float]]],
    *,
    min_quote_volume: float,
    min_depth: float,
    fee_bps: Mapping[str, float],
    slippage_bps: float,
    min_net_edge_bps: float,
    min_opportunities: int,
    min_forward_win_rate: float,
    holding_bars: int,
) -> PairScore | None:
    mids = {
        exchange: values
        for exchange, candles in candles_by_exchange.items()
        if (values := _candle_mids(candles))
    }
    if len(mids) < 2:
        return None
    common_timestamps = sorted(set.intersection(*(set(values) for values in mids.values())))
    if len(common_timestamps) <= holding_bars:
        return None

    gross_spreads: list[float] = []
    net_edges: list[float] = []
    opportunities: list[float] = []
    evaluable_count = len(common_timestamps) - holding_bars

    for index, timestamp in enumerate(common_timestamps):
        prices = {exchange: values[timestamp] for exchange, values in mids.items()}
        buy_exchange, buy_price = min(prices.items(), key=lambda item: item[1])
        sell_exchange, sell_price = max(prices.items(), key=lambda item: item[1])
        gross_spread_bps = (sell_price / buy_price - 1) * 10_000
        cost_bps = round_trip_cost_bps(
            buy_exchange,
            sell_exchange,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
        )
        gross_spreads.append(gross_spread_bps)
        net_edges.append(gross_spread_bps - cost_bps)

        if index >= evaluable_count or gross_spread_bps - cost_bps < min_net_edge_bps:
            continue
        future_timestamp = common_timestamps[index + holding_bars]
        future_buy = mids[buy_exchange][future_timestamp]
        future_sell = mids[sell_exchange][future_timestamp]
        long_return_bps = (future_buy / buy_price - 1) * 10_000
        short_return_bps = (sell_price - future_sell) / sell_price * 10_000
        opportunities.append(long_return_bps + short_return_bps - cost_bps)

    opportunity_count = len(opportunities)
    opportunity_rate = opportunity_count / evaluable_count
    mean_forward_pnl_bps = mean(opportunities) if opportunities else 0.0
    forward_win_rate = (
        sum(value > 0 for value in opportunities) / opportunity_count
        if opportunities
        else 0.0
    )
    score = (
        max(0.0, mean_forward_pnl_bps) * forward_win_rate * opportunity_rate
    )
    eligible = (
        opportunity_count >= min_opportunities
        and mean_forward_pnl_bps > 0
        and forward_win_rate >= min_forward_win_rate
    )
    return PairScore(
        symbol=symbol,
        exchanges=tuple(sorted(mids)),
        min_quote_volume=min_quote_volume,
        min_depth=min_depth,
        candle_count=len(common_timestamps),
        mean_gross_spread_bps=mean(gross_spreads),
        p95_gross_spread_bps=percentile(gross_spreads, 0.95),
        p95_net_edge_bps=percentile(net_edges, 0.95),
        max_gross_spread_bps=max(gross_spreads),
        opportunity_count=opportunity_count,
        opportunity_rate=opportunity_rate,
        mean_forward_pnl_bps=mean_forward_pnl_bps,
        forward_win_rate=forward_win_rate,
        score=score,
        eligible=eligible,
    )


class UniverseSelector:
    def __init__(
        self,
        *,
        clients: Mapping[str, Any],
        config: UniverseSelectorConfig,
        fee_bps: Mapping[str, float],
        slippage_bps: float,
    ) -> None:
        self.clients = dict(clients)
        self.config = config
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps
        self._semaphore = asyncio.Semaphore(config.concurrency)

    async def select(self) -> list[PairScore]:
        symbol_exchanges = spot_symbol_exchanges(
            {exchange: client.markets for exchange, client in self.clients.items()}
        )
        symbols = sorted(
            symbol
            for symbol, exchanges in symbol_exchanges.items()
            if len(exchanges) >= self.config.min_exchanges
        )
        logger.info(
            "universe: %d active spot USDT pairs listed on at least %d exchanges",
            len(symbols),
            self.config.min_exchanges,
        )
        if not symbols:
            return []

        tickers = await self._fetch_tickers(symbols)
        volume_candidates = []
        for symbol in symbols:
            supported_exchanges = symbol_exchanges[symbol]
            volumes = [
                quote_volume(tickers[exchange].get(symbol))
                for exchange in supported_exchanges
            ]
            min_volume = min(volumes, default=0.0)
            if min_volume >= self.config.min_quote_volume:
                volume_candidates.append((symbol, supported_exchanges, min_volume))
        volume_candidates.sort(key=lambda item: item[2], reverse=True)
        prefilter_limit = (
            self.config.max_history_pairs
            * self.config.liquidity_prefilter_multiplier
        )
        volume_candidates = volume_candidates[:prefilter_limit]
        logger.info(
            "universe: %d pairs passed volume, checking live depth",
            len(volume_candidates),
        )

        liquidity = await asyncio.gather(
            *(
                self._measure_liquidity(symbol, exchanges, min_volume)
                for symbol, exchanges, min_volume in volume_candidates
            )
        )
        liquid_pairs = [
            item
            for item in liquidity
            if item is not None and item.min_depth >= self.config.min_depth
        ]
        liquid_pairs.sort(
            key=lambda item: (item.min_quote_volume, item.min_depth),
            reverse=True,
        )
        liquid_pairs = liquid_pairs[: self.config.max_history_pairs]
        logger.info(
            "universe: %d pairs passed liquidity, downloading %s history",
            len(liquid_pairs),
            self.config.timeframe,
        )

        tasks = [
            asyncio.create_task(self._score_pair(pair))
            for pair in liquid_pairs
        ]
        result: list[PairScore] = []
        for completed, task in enumerate(asyncio.as_completed(tasks), start=1):
            score = await task
            logger.info(
                "universe history: %d/%d %s",
                completed,
                len(tasks),
                score.symbol if score is not None else "skipped",
            )
            if score is not None:
                result.append(score)
        result.sort(
            key=lambda item: (item.eligible, item.score, item.p95_net_edge_bps),
            reverse=True,
        )
        return result

    async def _fetch_tickers(
        self,
        symbols: Sequence[str],
    ) -> dict[str, Mapping[str, Mapping[str, Any]]]:
        async def fetch(exchange: str) -> tuple[str, Mapping[str, Mapping[str, Any]]]:
            client = self.clients[exchange]
            try:
                async with self._semaphore:
                    return exchange, await client.fetch_tickers()
            except Exception as error:
                logger.warning("%s fetch_tickers failed: %s", exchange, error)
                fallback: dict[str, Mapping[str, Any]] = {}
                for symbol in symbols:
                    try:
                        async with self._semaphore:
                            fallback[symbol] = await client.fetch_ticker(symbol)
                    except Exception as ticker_error:
                        logger.debug(
                            "%s %s ticker unavailable: %s",
                            exchange,
                            symbol,
                            ticker_error,
                        )
                return exchange, fallback

        values = await asyncio.gather(*(fetch(exchange) for exchange in self.config.exchanges))
        return dict(values)

    async def _measure_liquidity(
        self,
        symbol: str,
        exchanges: Sequence[str],
        min_volume: float,
    ) -> PairLiquidity | None:
        depths: dict[str, float] = {}
        for exchange in exchanges:
            try:
                async with self._semaphore:
                    order_book = await self.clients[exchange].fetch_order_book(
                        symbol,
                        limit=50,
                    )
                depths[exchange] = depth_within_band(
                    order_book,
                    self.config.depth_band_bps,
                )
            except Exception as error:
                logger.debug("%s %s depth unavailable: %s", exchange, symbol, error)
        if len(depths) < self.config.min_exchanges:
            return None
        return PairLiquidity(
            symbol=symbol,
            exchanges=tuple(depths),
            min_quote_volume=min_volume,
            min_depth=min(depths.values(), default=0.0),
        )

    async def _score_pair(self, liquidity: PairLiquidity) -> PairScore | None:
        candles = await asyncio.gather(
            *(
                self._fetch_history(exchange, liquidity.symbol)
                for exchange in liquidity.exchanges
            )
        )
        candles_by_exchange = dict(zip(liquidity.exchanges, candles, strict=True))
        return score_pair_history(
            liquidity.symbol,
            candles_by_exchange,
            min_quote_volume=liquidity.min_quote_volume,
            min_depth=liquidity.min_depth,
            fee_bps=self.fee_bps,
            slippage_bps=self.slippage_bps,
            min_net_edge_bps=self.config.min_net_edge_bps,
            min_opportunities=self.config.min_opportunities,
            min_forward_win_rate=self.config.min_forward_win_rate,
            holding_bars=self.config.holding_bars,
        )

    async def _fetch_history(
        self,
        exchange: str,
        symbol: str,
    ) -> list[list[float]]:
        client = self.clients[exchange]
        until_ms = int(time.time() * 1_000)
        since_ms = until_ms - self.config.days * 24 * 60 * 60 * 1_000
        timeframe_ms = int(client.parse_timeframe(self.config.timeframe) * 1_000)
        cursor = since_ms
        candles: dict[int, list[float]] = {}
        try:
            while cursor < until_ms:
                async with self._semaphore:
                    batch = await client.fetch_ohlcv(
                        symbol,
                        self.config.timeframe,
                        since=cursor,
                        limit=1_000,
                    )
                if not batch:
                    break
                for candle in batch:
                    timestamp = int(candle[0])
                    if timestamp <= until_ms:
                        candles[timestamp] = list(candle)
                next_cursor = int(batch[-1][0]) + timeframe_ms
                if next_cursor <= cursor:
                    break
                cursor = next_cursor
                if int(batch[-1][0]) >= until_ms - timeframe_ms:
                    break
        except Exception as error:
            logger.warning("%s %s OHLCV failed: %s", exchange, symbol, error)
            return []
        return [candles[timestamp] for timestamp in sorted(candles)]
