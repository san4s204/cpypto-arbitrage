from pathlib import Path

from app.analytics.storage import TradeStore
from tests.helpers import closed_trade


def test_trade_store_restores_paper_history(tmp_path: Path) -> None:
    store = TradeStore(tmp_path / "paper.sqlite3")
    expected = closed_trade(trade_id="persisted", pnl=12.5)

    store.save_trade(expected)
    actual = store.load_trades()

    assert len(actual) == 1
    assert actual[0] == expected
