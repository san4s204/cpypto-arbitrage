import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.domain.models import Quote
from app.research.latency_routes import analyze_latency_route
from app.research.live_report import analyze_live_pair
from app.research.live_store import LiveQuoteStore, StoredFrame, StoredQuote
from app.research.record_live import (
    RecorderCounters,
    _record_synchronized_frames,
    load_candidate_symbols,
)

BASE_TIME = datetime.now(UTC)


def stored_quote(
    exchange: str,
    *,
    symbol: str = "TEST/USDT",
    mid: float,
    seconds: float = 0,
    with_features: bool = False,
) -> StoredQuote:
    timestamp = BASE_TIME + timedelta(seconds=seconds)
    return StoredQuote(
        symbol=symbol,
        exchange=exchange,
        bid=mid - 0.01,
        ask=mid + 0.01,
        occurred_at=timestamp,
        received_at=timestamp,
        bid_size=80 if with_features else None,
        ask_size=20 if with_features else None,
        buy_volume=800 if with_features else None,
        sell_volume=200 if with_features else None,
        trade_flow_window_seconds=60 if with_features else None,
    )


def frame(seconds: float, **prices: float) -> StoredFrame:
    return StoredFrame(
        sampled_at_ms=int((BASE_TIME + timedelta(seconds=seconds)).timestamp() * 1_000),
        quotes={
            exchange: stored_quote(exchange, mid=price, seconds=seconds)
            for exchange, price in prices.items()
        },
    )


def test_live_store_persists_a_synchronized_recording_session(tmp_path: Path) -> None:
    store = LiveQuoteStore(tmp_path / "live.sqlite3", flush_rows=100)
    store.start_session(
        session_id="session",
        started_at=BASE_TIME,
        sample_interval_seconds=5,
        symbols=("TEST/USDT",),
        exchanges=("bybit", "okx"),
    )
    store.append_frame(
        session_id="session",
        sampled_at=BASE_TIME,
        quotes=[
            stored_quote("bybit", mid=100, with_features=True),
            stored_quote("okx", mid=101),
        ],
    )
    store.finish_session(
        session_id="session",
        ended_at=BASE_TIME + timedelta(seconds=5),
        attempted_samples=1,
        recorded_frames=1,
    )

    session = store.get_session("session")
    values = list(store.iter_symbol_frames("session"))
    store.close()

    assert session.attempted_samples == 1
    assert session.recorded_frames == 1
    assert len(values) == 1
    assert values[0][0] == "TEST/USDT"
    assert set(values[0][1][0].quotes) == {"bybit", "okx"}
    assert values[0][1][0].quotes["bybit"].bid_size == 80
    assert values[0][1][0].quotes["bybit"].buy_volume == 800

    with LiveQuoteStore(tmp_path / "live.sqlite3", read_only=True) as reader:
        assert [item.session_id for item in reader.list_sessions()] == ["session"]


def test_live_store_migrates_the_previous_quote_schema(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE live_quotes (
            session_id TEXT NOT NULL,
            sampled_at_ms INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            exchange TEXT NOT NULL,
            bid REAL NOT NULL,
            ask REAL NOT NULL,
            occurred_at_ms INTEGER NOT NULL,
            received_at_ms INTEGER NOT NULL,
            PRIMARY KEY (session_id, sampled_at_ms, symbol, exchange)
        )
        """
    )
    connection.commit()
    connection.close()

    with LiveQuoteStore(path):
        pass

    connection = sqlite3.connect(path)
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(live_quotes)")
    }
    connection.close()

    assert {
        "bid_size",
        "ask_size",
        "buy_volume",
        "sell_volume",
        "trade_flow_window_seconds",
    } <= columns


def test_recorder_keeps_a_pair_available_on_two_of_three_exchanges(tmp_path: Path) -> None:
    path = tmp_path / "partial.sqlite3"
    store = LiveQuoteStore(path)
    store.start_session(
        session_id="session",
        started_at=BASE_TIME,
        sample_interval_seconds=0.5,
        symbols=("TEST/USDT",),
        exchanges=("bybit", "okx", "bitget"),
    )
    latest = {
        (exchange, "TEST/USDT"): Quote(
            exchange=exchange,
            symbol="TEST/USDT",
            bid=99.9,
            ask=100.1,
            occurred_at=BASE_TIME,
            received_at=BASE_TIME,
        )
        for exchange in ("bybit", "bitget")
    }
    counters = RecorderCounters()

    _record_synchronized_frames(
        store=store,
        session_id="session",
        latest=latest,
        exchanges=("bybit", "okx", "bitget"),
        symbols=("TEST/USDT",),
        min_exchanges=2,
        max_quote_age_seconds=2,
        counters=counters,
    )
    store.finish_session(
        session_id="session",
        ended_at=BASE_TIME + timedelta(seconds=0.5),
        attempted_samples=counters.attempted_samples,
        recorded_frames=counters.recorded_frames,
    )
    values = list(store.iter_symbol_frames("session"))
    store.close()

    assert counters.recorded_frames == 1
    assert set(values[0][1][0].quotes) == {"bybit", "bitget"}


def test_live_report_simulates_profitable_spread_convergence() -> None:
    frames = [
        StoredFrame(
            sampled_at_ms=int(BASE_TIME.timestamp() * 1_000),
            quotes={
                "bybit": StoredQuote(
                    "TEST/USDT",
                    "bybit",
                    99.9,
                    100,
                    BASE_TIME,
                    BASE_TIME,
                ),
                "okx": StoredQuote(
                    "TEST/USDT",
                    "okx",
                    101,
                    101.1,
                    BASE_TIME,
                    BASE_TIME,
                ),
            },
        ),
        StoredFrame(
            sampled_at_ms=int((BASE_TIME + timedelta(seconds=60)).timestamp() * 1_000),
            quotes={
                "bybit": StoredQuote(
                    "TEST/USDT",
                    "bybit",
                    100.5,
                    100.6,
                    BASE_TIME,
                    BASE_TIME,
                ),
                "okx": StoredQuote(
                    "TEST/USDT",
                    "okx",
                    100.6,
                    100.7,
                    BASE_TIME,
                    BASE_TIME,
                ),
            },
        ),
    ]

    metrics = analyze_live_pair(
        "TEST/USDT",
        frames,
        attempted_samples=2,
        sample_interval_seconds=60,
        fee_bps={"bybit": 0, "okx": 0},
        slippage_bps=0,
        spread_entry_bps=50,
        spread_exit_bps=5,
        min_spread_trades=1,
    )

    assert metrics.coverage == 1
    assert metrics.spread_trades == 1
    assert metrics.spread_average_pnl_bps > 0
    assert metrics.spread_win_rate == 1
    assert metrics.spread_candidate


def test_live_report_detects_a_follower_response() -> None:
    frames = [
        frame(0, bybit=100, okx=100),
        frame(5, bybit=101, okx=100),
        frame(10, bybit=101, okx=100.1),
    ]

    metrics = analyze_live_pair(
        "TEST/USDT",
        frames,
        attempted_samples=3,
        sample_interval_seconds=5,
        fee_bps={"bybit": 10, "okx": 10},
        slippage_bps=2,
    )

    assert metrics.latency_events == 1
    assert metrics.latency_follow_rate == 1
    assert metrics.latency_median_delay_seconds == 5


def test_directed_latency_report_includes_cost_aware_route_metrics() -> None:
    frames = [
        frame(0, bybit=100, bitget=100),
        frame(5, bybit=101, bitget=100),
        frame(10, bybit=101, bitget=100.1),
    ]

    metrics = analyze_latency_route(
        session_id="session",
        symbol="TEST/USDT",
        leader_exchange="bybit",
        follower_exchange="bitget",
        frames=frames,
        sample_interval_seconds=5,
        fee_bps={"bybit": 0, "bitget": 0},
        slippage_bps=0,
        min_trades=1,
    )

    assert metrics.events == 1
    assert metrics.follow_rate == 1
    assert metrics.median_delay_seconds == 5
    assert metrics.trade_count == 1
    assert metrics.expectancy_bps > 0


def test_candidate_loader_keeps_csv_ranking_order(tmp_path: Path) -> None:
    report = tmp_path / "candidates.csv"
    report.write_text(
        "symbol,score\nTRUMP/USDT,3\nGRAM/USDT,2\nLIT/USDT,1\n",
        encoding="utf-8",
    )

    assert load_candidate_symbols(report, top=2) == (
        "TRUMP/USDT",
        "GRAM/USDT",
    )
