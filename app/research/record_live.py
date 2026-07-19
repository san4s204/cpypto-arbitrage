from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.config.settings import AppSettings
from app.domain.models import Quote, utc_now
from app.market_data.ws_listener import CcxtProMarketDataFeed
from app.research.live_report import (
    LivePairMetrics,
    analyze_live_pair,
    print_live_rankings,
    write_live_report,
)
from app.research.live_store import LiveQuoteStore, StoredQuote

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RecorderCounters:
    quote_updates: int = 0
    attempted_samples: int = 0
    recorded_frames: int = 0


def _csv_values(raw: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in raw.split(",") if value.strip())


def load_candidate_symbols(
    path: Path,
    *,
    top: int,
) -> tuple[str, ...]:
    if not path.exists():
        return ()
    with path.open(encoding="utf-8", newline="") as file:
        rows = csv.DictReader(file)
        symbols = [
            str(row["symbol"]).strip()
            for row in rows
            if row.get("symbol") and str(row["symbol"]).strip()
        ]
    return tuple(dict.fromkeys(symbols[:top]))


async def _consume_quotes(
    feed: CcxtProMarketDataFeed,
    latest: dict[tuple[str, str], Quote],
    counters: RecorderCounters,
) -> None:
    async for quote in feed.quotes():
        latest[(quote.exchange, quote.symbol)] = quote
        counters.quote_updates += 1


def _record_synchronized_frames(
    *,
    store: LiveQuoteStore,
    session_id: str,
    latest: dict[tuple[str, str], Quote],
    exchanges: tuple[str, ...],
    symbols: tuple[str, ...],
    max_quote_age_seconds: float,
    counters: RecorderCounters,
) -> None:
    sampled_at = utc_now()
    counters.attempted_samples += 1
    for symbol in symbols:
        quotes = [latest.get((exchange, symbol)) for exchange in exchanges]
        if any(quote is None for quote in quotes):
            continue
        complete_quotes = [quote for quote in quotes if quote is not None]
        if any(
            quote.age_seconds(sampled_at) > max_quote_age_seconds
            or (sampled_at - quote.received_at).total_seconds() > max_quote_age_seconds
            for quote in complete_quotes
        ):
            continue
        store.append_frame(
            session_id=session_id,
            sampled_at=sampled_at,
            quotes=[
                StoredQuote(
                    symbol=quote.symbol,
                    exchange=quote.exchange,
                    bid=quote.bid,
                    ask=quote.ask,
                    occurred_at=quote.occurred_at,
                    received_at=quote.received_at,
                    bid_size=quote.bid_size,
                    ask_size=quote.ask_size,
                    buy_volume=quote.buy_volume,
                    sell_volume=quote.sell_volume,
                    trade_flow_window_seconds=quote.trade_flow_window_seconds,
                )
                for quote in complete_quotes
            ],
        )
        counters.recorded_frames += 1


def _build_report(
    *,
    store: LiveQuoteStore,
    session_id: str,
    fee_bps: dict[str, float],
    slippage_bps: float,
    output_path: Path,
    top: int,
    spread_entry_bps: float,
    spread_exit_bps: float,
    spread_max_hold_seconds: float,
) -> list[LivePairMetrics]:
    session = store.get_session(session_id)
    metrics = [
        analyze_live_pair(
            symbol,
            frames,
            attempted_samples=session.attempted_samples,
            sample_interval_seconds=session.sample_interval_seconds,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            spread_entry_bps=spread_entry_bps,
            spread_exit_bps=spread_exit_bps,
            spread_max_hold_seconds=spread_max_hold_seconds,
        )
        for symbol, frames in store.iter_symbol_frames(session_id)
        if frames
    ]
    if not metrics:
        logger.warning("no synchronized frames were recorded; report is empty")
        return []
    metrics.sort(
        key=lambda item: max(
            item.spread_score,
            item.micro_score,
            item.latency_score,
        ),
        reverse=True,
    )
    write_live_report(output_path, metrics)
    print_live_rankings(metrics, top=top)
    print(f"\nLive report: {output_path}")
    return metrics


async def run(args: argparse.Namespace) -> None:
    settings = AppSettings.from_env()
    exchanges = (
        _csv_values(args.exchanges) if args.exchanges else settings.exchanges
    )
    missing_fees = [
        exchange for exchange in exchanges if exchange not in settings.paper_fee_bps
    ]
    if missing_fees:
        raise ValueError(
            "PAPER_FEE_BPS is missing exchanges: " + ", ".join(missing_fees)
        )

    candidate_path = Path(args.candidates)
    if not candidate_path.is_absolute():
        candidate_path = settings.project_root / candidate_path
    symbols = (
        _csv_values(args.symbols)
        if args.symbols
        else load_candidate_symbols(candidate_path, top=args.top)
    )
    if not symbols:
        logger.warning(
            "candidate CSV is unavailable or empty; using SYMBOLS from .env"
        )
        symbols = settings.symbols
    if not symbols:
        raise ValueError("at least one symbol is required for live recording")

    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = settings.project_root / db_path
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = settings.project_root / output_path

    session_id = uuid4().hex
    store = LiveQuoteStore(db_path)
    store.start_session(
        session_id=session_id,
        started_at=utc_now(),
        sample_interval_seconds=args.sample_seconds,
        symbols=symbols,
        exchanges=exchanges,
    )
    feed = CcxtProMarketDataFeed(
        exchanges=exchanges,
        symbols=symbols,
        trade_flow_window_seconds=args.trade_flow_window_seconds,
    )
    latest: dict[tuple[str, str], Quote] = {}
    counters = RecorderCounters()
    consumer = asyncio.create_task(_consume_quotes(feed, latest, counters))
    started = time.monotonic()
    deadline = started + args.hours * 3_600 if args.hours > 0 else None
    last_heartbeat = started
    cancelled = False

    logger.info(
        "live recording started: session=%s exchanges=%s symbols=%d interval=%.1fs "
        "duration=%s",
        session_id[:8],
        ",".join(exchanges),
        len(symbols),
        args.sample_seconds,
        f"{args.hours:g}h" if args.hours > 0 else "until Ctrl+C",
    )
    logger.info("symbols: %s", ",".join(symbols))
    try:
        while deadline is None or time.monotonic() < deadline:
            if consumer.done():
                consumer.result()
            remaining = (
                max(0.0, deadline - time.monotonic())
                if deadline is not None
                else args.sample_seconds
            )
            await asyncio.sleep(min(args.sample_seconds, remaining))
            if deadline is not None and time.monotonic() >= deadline:
                break
            _record_synchronized_frames(
                store=store,
                session_id=session_id,
                latest=latest,
                exchanges=exchanges,
                symbols=symbols,
                max_quote_age_seconds=args.max_quote_age_seconds,
                counters=counters,
            )
            now = time.monotonic()
            if now - last_heartbeat >= args.heartbeat_seconds:
                store.flush()
                expected_frames = counters.attempted_samples * len(symbols)
                coverage = (
                    counters.recorded_frames / expected_frames
                    if expected_frames
                    else 0.0
                )
                feature_streams = sum(
                    quote.book_imbalance is not None
                    and quote.trade_flow_imbalance is not None
                    for quote in latest.values()
                )
                logger.info(
                    "recording heartbeat: elapsed=%.1fh quotes=%d attempts=%d "
                    "frames=%d coverage=%.1f%% streams=%d/%d features=%d/%d",
                    (now - started) / 3_600,
                    counters.quote_updates,
                    counters.attempted_samples,
                    counters.recorded_frames,
                    coverage * 100,
                    len(latest),
                    len(exchanges) * len(symbols),
                    feature_streams,
                    len(exchanges) * len(symbols),
                )
                last_heartbeat = now
    except asyncio.CancelledError:
        cancelled = True
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        store.finish_session(
            session_id=session_id,
            ended_at=utc_now(),
            attempted_samples=counters.attempted_samples,
            recorded_frames=counters.recorded_frames,
        )
        try:
            _build_report(
                store=store,
                session_id=session_id,
                fee_bps=settings.paper_fee_bps,
                slippage_bps=settings.paper_slippage_bps,
                output_path=output_path,
                top=args.report_top,
                spread_entry_bps=args.spread_entry_bps,
                spread_exit_bps=args.spread_exit_bps,
                spread_max_hold_seconds=args.spread_max_hold_seconds,
            )
        finally:
            store.close()
    if cancelled:
        raise asyncio.CancelledError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record synchronized live quotes and rank pairs for each strategy"
    )
    parser.add_argument("--hours", type=float, default=10)
    parser.add_argument("--sample-seconds", type=float, default=5)
    parser.add_argument("--trade-flow-window-seconds", type=float, default=60)
    parser.add_argument("--heartbeat-seconds", type=float, default=60)
    parser.add_argument("--max-quote-age-seconds", type=float, default=15)
    parser.add_argument("--top", type=int, default=20, help="pairs read from candidate CSV")
    parser.add_argument("--report-top", type=int, default=10)
    parser.add_argument("--symbols", help="explicit comma-separated symbols")
    parser.add_argument("--exchanges", help="comma-separated exchanges; default: EXCHANGES")
    parser.add_argument(
        "--candidates",
        default="runtime/universe_candidates.csv",
    )
    parser.add_argument("--db", default="runtime/live_quotes.sqlite3")
    parser.add_argument("--output", default="runtime/live_universe_report.csv")
    parser.add_argument("--spread-entry-bps", type=float, default=65)
    parser.add_argument("--spread-exit-bps", type=float, default=8)
    parser.add_argument("--spread-max-hold-seconds", type=float, default=300)
    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = build_parser().parse_args()
    if args.hours < 0:
        raise ValueError("hours cannot be negative")
    for name in (
        "sample_seconds",
        "trade_flow_window_seconds",
        "heartbeat_seconds",
        "max_quote_age_seconds",
        "top",
        "report_top",
        "spread_max_hold_seconds",
    ):
        if getattr(args, name) <= 0:
            raise ValueError(f"{name} must be positive")
    with suppress(KeyboardInterrupt):
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
