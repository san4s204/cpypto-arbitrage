from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from app.domain.models import ClosedTrade, PositionSide


class TradeStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_trades (
                    trade_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    entry_notional REAL NOT NULL,
                    gross_pnl REAL NOT NULL,
                    fees REAL NOT NULL,
                    net_pnl REAL NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL,
                    close_reason TEXT NOT NULL,
                    group_id TEXT
                )
                """
            )

    def save_trade(self, trade: ClosedTrade) -> None:
        payload = asdict(trade)
        payload["side"] = trade.side.value
        payload["opened_at"] = trade.opened_at.isoformat()
        payload["closed_at"] = trade.closed_at.isoformat()
        columns = tuple(payload)
        placeholders = ", ".join("?" for _ in columns)
        with self._connect() as connection:
            connection.execute(
                f"INSERT OR IGNORE INTO paper_trades ({', '.join(columns)}) "
                f"VALUES ({placeholders})",
                tuple(payload[column] for column in columns),
            )

    def load_trades(self) -> list[ClosedTrade]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM paper_trades ORDER BY closed_at, trade_id"
            ).fetchall()
        return [
            ClosedTrade(
                trade_id=row["trade_id"],
                strategy_id=row["strategy_id"],
                exchange=row["exchange"],
                symbol=row["symbol"],
                side=PositionSide(row["side"]),
                quantity=row["quantity"],
                entry_price=row["entry_price"],
                exit_price=row["exit_price"],
                entry_notional=row["entry_notional"],
                gross_pnl=row["gross_pnl"],
                fees=row["fees"],
                net_pnl=row["net_pnl"],
                opened_at=datetime.fromisoformat(row["opened_at"]),
                closed_at=datetime.fromisoformat(row["closed_at"]),
                close_reason=row["close_reason"],
                group_id=row["group_id"],
            )
            for row in rows
        ]
