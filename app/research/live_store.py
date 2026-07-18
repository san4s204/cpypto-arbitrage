from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StoredQuote:
    symbol: str
    exchange: str
    bid: float
    ask: float
    occurred_at: datetime
    received_at: datetime


@dataclass(frozen=True, slots=True)
class StoredFrame:
    sampled_at_ms: int
    quotes: dict[str, StoredQuote]


@dataclass(frozen=True, slots=True)
class RecordingSession:
    session_id: str
    started_at_ms: int
    ended_at_ms: int | None
    sample_interval_seconds: float
    symbols: tuple[str, ...]
    exchanges: tuple[str, ...]
    attempted_samples: int
    recorded_frames: int


def _to_ms(value: datetime) -> int:
    return int(value.timestamp() * 1_000)


class LiveQuoteStore:
    def __init__(self, path: Path, *, flush_rows: int = 400) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.flush_rows = flush_rows
        self._connection = sqlite3.connect(path)
        self._pending: list[tuple] = []
        self._initialize()

    def _initialize(self) -> None:
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS recording_sessions (
                session_id TEXT PRIMARY KEY,
                started_at_ms INTEGER NOT NULL,
                ended_at_ms INTEGER,
                sample_interval_seconds REAL NOT NULL,
                symbols TEXT NOT NULL,
                exchanges TEXT NOT NULL,
                attempted_samples INTEGER NOT NULL DEFAULT 0,
                recorded_frames INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS live_quotes (
                session_id TEXT NOT NULL,
                sampled_at_ms INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                bid REAL NOT NULL,
                ask REAL NOT NULL,
                occurred_at_ms INTEGER NOT NULL,
                received_at_ms INTEGER NOT NULL,
                PRIMARY KEY (session_id, sampled_at_ms, symbol, exchange),
                FOREIGN KEY (session_id) REFERENCES recording_sessions(session_id)
            );

            CREATE INDEX IF NOT EXISTS ix_live_quotes_session_symbol_time
            ON live_quotes (session_id, symbol, sampled_at_ms);
            """
        )
        self._connection.commit()

    def start_session(
        self,
        *,
        session_id: str,
        started_at: datetime,
        sample_interval_seconds: float,
        symbols: Sequence[str],
        exchanges: Sequence[str],
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO recording_sessions (
                    session_id,
                    started_at_ms,
                    sample_interval_seconds,
                    symbols,
                    exchanges
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    _to_ms(started_at),
                    sample_interval_seconds,
                    ",".join(symbols),
                    ",".join(exchanges),
                ),
            )

    def append_frame(
        self,
        *,
        session_id: str,
        sampled_at: datetime,
        quotes: Sequence[StoredQuote],
    ) -> None:
        sampled_at_ms = _to_ms(sampled_at)
        self._pending.extend(
            (
                session_id,
                sampled_at_ms,
                quote.symbol,
                quote.exchange,
                quote.bid,
                quote.ask,
                _to_ms(quote.occurred_at),
                _to_ms(quote.received_at),
            )
            for quote in quotes
        )
        if len(self._pending) >= self.flush_rows:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        with self._connection:
            self._connection.executemany(
                """
                INSERT OR REPLACE INTO live_quotes (
                    session_id,
                    sampled_at_ms,
                    symbol,
                    exchange,
                    bid,
                    ask,
                    occurred_at_ms,
                    received_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._pending,
            )
        self._pending.clear()

    def finish_session(
        self,
        *,
        session_id: str,
        ended_at: datetime,
        attempted_samples: int,
        recorded_frames: int,
    ) -> None:
        self.flush()
        with self._connection:
            self._connection.execute(
                """
                UPDATE recording_sessions
                SET ended_at_ms = ?, attempted_samples = ?, recorded_frames = ?
                WHERE session_id = ?
                """,
                (
                    _to_ms(ended_at),
                    attempted_samples,
                    recorded_frames,
                    session_id,
                ),
            )

    def get_session(self, session_id: str) -> RecordingSession:
        row = self._connection.execute(
            """
            SELECT * FROM recording_sessions WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"recording session {session_id!r} not found")
        return RecordingSession(
            session_id=str(row[0]),
            started_at_ms=int(row[1]),
            ended_at_ms=int(row[2]) if row[2] is not None else None,
            sample_interval_seconds=float(row[3]),
            symbols=tuple(value for value in str(row[4]).split(",") if value),
            exchanges=tuple(value for value in str(row[5]).split(",") if value),
            attempted_samples=int(row[6]),
            recorded_frames=int(row[7]),
        )

    def iter_symbol_frames(
        self,
        session_id: str,
    ) -> Iterator[tuple[str, list[StoredFrame]]]:
        cursor = self._connection.execute(
            """
            SELECT
                sampled_at_ms,
                symbol,
                exchange,
                bid,
                ask,
                occurred_at_ms,
                received_at_ms
            FROM live_quotes
            WHERE session_id = ?
            ORDER BY symbol, sampled_at_ms, exchange
            """,
            (session_id,),
        )
        current_symbol: str | None = None
        current_timestamp: int | None = None
        current_quotes: dict[str, StoredQuote] = {}
        frames: list[StoredFrame] = []

        def finish_frame() -> None:
            if current_timestamp is not None and current_quotes:
                frames.append(StoredFrame(current_timestamp, dict(current_quotes)))

        for row in cursor:
            sampled_at_ms = int(row[0])
            symbol = str(row[1])
            if current_symbol is not None and symbol != current_symbol:
                finish_frame()
                yield current_symbol, frames
                frames = []
                current_quotes = {}
                current_timestamp = None
            if current_timestamp is not None and sampled_at_ms != current_timestamp:
                finish_frame()
                current_quotes = {}
            current_symbol = symbol
            current_timestamp = sampled_at_ms
            exchange = str(row[2])
            current_quotes[exchange] = StoredQuote(
                symbol=symbol,
                exchange=exchange,
                bid=float(row[3]),
                ask=float(row[4]),
                occurred_at=datetime.fromtimestamp(int(row[5]) / 1_000, tz=UTC),
                received_at=datetime.fromtimestamp(int(row[6]) / 1_000, tz=UTC),
            )

        if current_symbol is not None:
            finish_frame()
            yield current_symbol, frames

    def close(self) -> None:
        self.flush()
        self._connection.close()

    def __enter__(self) -> LiveQuoteStore:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
