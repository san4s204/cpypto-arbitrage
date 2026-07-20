from app.main import _quote_snapshot
from tests.helpers import quote


def test_quote_snapshot_shows_each_live_market() -> None:
    quotes = {
        ("okx", "ETH/USDT"): quote(
            "okx",
            symbol="ETH/USDT",
            bid=3_500,
            ask=3_501,
        ),
        ("bybit", "BTC/USDT"): quote(
            "bybit",
            symbol="BTC/USDT",
            bid=100_000,
            ask=100_002,
        ),
    }

    assert _quote_snapshot(quotes) == (
        "bybit BTC/USDT=100001, okx ETH/USDT=3500.5"
    )
