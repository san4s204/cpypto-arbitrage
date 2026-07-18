import pytest

from app.research.universe_selector import (
    UniverseSelector,
    UniverseSelectorConfig,
    common_spot_symbols,
    depth_within_band,
    round_trip_cost_bps,
    score_pair_history,
)


def candle(timestamp: int, price: float) -> list[float]:
    return [timestamp, price, price, price, price, 1]


class FakeExchange:
    def __init__(self, prices: list[float]) -> None:
        self.markets = {
            "TEST/USDT": {
                "symbol": "TEST/USDT",
                "spot": True,
                "active": True,
            }
        }
        self.prices = prices

    async def fetch_tickers(self) -> dict:
        return {"TEST/USDT": {"quoteVolume": 1_000_000}}

    async def fetch_order_book(self, symbol: str, *, limit: int) -> dict:
        return {
            "bids": [[self.prices[0], 100]],
            "asks": [[self.prices[0], 100]],
        }

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        *,
        since: int,
        limit: int,
    ) -> list[list[float]]:
        return [candle(index * 60_000, price) for index, price in enumerate(self.prices)]

    def parse_timeframe(self, timeframe: str) -> int:
        return 60


def test_common_spot_symbols_excludes_stables_leverage_and_inactive_markets() -> None:
    markets = {
        "bybit": {
            "btc": {"symbol": "BTC/USDT", "spot": True, "active": True},
            "eth": {"symbol": "ETH/USDT", "spot": True, "active": True},
            "stable": {"symbol": "USDC/USDT", "spot": True, "active": True},
            "leveraged": {"symbol": "BTC3L/USDT", "spot": True, "active": True},
            "inactive": {"symbol": "OLD/USDT", "spot": True, "active": False},
        },
        "okx": {
            "btc": {"symbol": "BTC/USDT", "spot": True, "active": True},
            "eth": {"symbol": "ETH/USDT", "spot": True, "active": True},
            "stable": {"symbol": "USDC/USDT", "spot": True, "active": True},
            "leveraged": {"symbol": "BTC3L/USDT", "spot": True, "active": True},
        },
    }

    assert common_spot_symbols(markets) == ["BTC/USDT", "ETH/USDT"]


def test_depth_uses_the_weaker_book_side_inside_price_band() -> None:
    order_book = {
        "bids": [[100, 10], [99.95, 20], [99, 1_000]],
        "asks": [[100.1, 5], [100.15, 5], [102, 1_000]],
    }

    assert depth_within_band(order_book, 10) == pytest.approx(1_001.25)


def test_historical_score_includes_full_round_trip_cost_and_convergence() -> None:
    score = score_pair_history(
        "TEST/USDT",
        {
            "bybit": [candle(0, 100), candle(1, 101), candle(2, 101)],
            "okx": [candle(0, 102), candle(1, 101), candle(2, 101)],
        },
        min_quote_volume=1_000_000,
        min_depth=10_000,
        fee_bps={"bybit": 10, "okx": 10},
        slippage_bps=0,
        min_net_edge_bps=5,
        min_opportunities=1,
        min_forward_win_rate=0.5,
        holding_bars=1,
    )

    assert score is not None
    assert score.opportunity_count == 1
    assert score.opportunity_rate == 0.5
    assert score.mean_forward_pnl_bps == pytest.approx(158.0392, rel=1e-4)
    assert score.forward_win_rate == 1
    assert score.eligible


def test_small_nominal_spread_is_rejected_after_costs() -> None:
    score = score_pair_history(
        "QUIET/USDT",
        {
            "bybit": [candle(0, 100), candle(1, 100), candle(2, 100)],
            "okx": [candle(0, 100.2), candle(1, 100.2), candle(2, 100.2)],
        },
        min_quote_volume=1_000_000,
        min_depth=10_000,
        fee_bps={"bybit": 10, "okx": 10},
        slippage_bps=2,
        min_net_edge_bps=5,
        min_opportunities=1,
        min_forward_win_rate=0.5,
        holding_bars=1,
    )

    assert score is not None
    assert score.opportunity_count == 0
    assert not score.eligible
    assert round_trip_cost_bps(
        "bybit",
        "okx",
        fee_bps={"bybit": 10, "okx": 10},
        slippage_bps=2,
    ) == 48


@pytest.mark.asyncio
async def test_selector_runs_liquidity_and_history_stages() -> None:
    selector = UniverseSelector(
        clients={
            "bybit": FakeExchange([100, 101, 101]),
            "okx": FakeExchange([102, 101, 101]),
        },
        config=UniverseSelectorConfig(
            exchanges=("bybit", "okx"),
            days=1,
            timeframe="1m",
            max_history_pairs=1,
            min_quote_volume=200_000,
            min_depth=2_000,
            min_net_edge_bps=5,
            min_opportunities=1,
            holding_bars=1,
        ),
        fee_bps={"bybit": 10, "okx": 10},
        slippage_bps=0,
    )

    scores = await selector.select()

    assert len(scores) == 1
    assert scores[0].symbol == "TEST/USDT"
    assert scores[0].eligible
