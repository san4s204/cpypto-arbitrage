from __future__ import annotations

import argparse
import asyncio
import csv
import logging
from pathlib import Path

from dotenv import set_key

from app.config.settings import AppSettings
from app.research.universe_selector import (
    PairScore,
    UniverseSelector,
    UniverseSelectorConfig,
)

logger = logging.getLogger(__name__)


def _csv_values(raw: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in raw.split(",") if value.strip())


def _write_report(path: Path, scores: list[PairScore]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(scores[0].as_row()))
        writer.writeheader()
        writer.writerows(score.as_row() for score in scores)


def _print_ranking(scores: list[PairScore], top: int) -> None:
    print("\nHistorical universe ranking")
    print(
        f"{'#':>2}  {'symbol':<14} {'venues':<18} {'vol min':>10} {'depth':>9} "
        f"{'p95 edge':>9} {'events':>7} {'fwd pnl':>9} {'wins':>7} {'ok':>3}"
    )
    for index, score in enumerate(scores[:top], start=1):
        print(
            f"{index:>2}  {score.symbol:<14} {','.join(score.exchanges):<18} "
            f"{score.min_quote_volume / 1_000_000:>8.2f}M "
            f"{score.min_depth:>9.0f} "
            f"{score.p95_net_edge_bps:>8.1f}b "
            f"{score.opportunity_count:>7} "
            f"{score.mean_forward_pnl_bps:>8.1f}b "
            f"{score.forward_win_rate:>6.1%} "
            f"{'yes' if score.eligible else 'no':>3}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rank common liquid spot-USDT pairs by historical cross-exchange spread"
    )
    parser.add_argument("--exchanges", help="comma-separated exchange ids; default: EXCHANGES")
    parser.add_argument(
        "--min-exchanges",
        type=int,
        default=2,
        help="minimum venues listing a pair",
    )
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--history-pairs", type=int, default=40)
    parser.add_argument("--top", type=int, default=8)
    parser.add_argument("--min-volume", type=float, default=200_000)
    parser.add_argument("--min-depth", type=float, default=2_000)
    parser.add_argument("--depth-band-bps", type=float, default=10)
    parser.add_argument("--min-edge-bps", type=float, default=5)
    parser.add_argument("--min-opportunities", type=int, default=3)
    parser.add_argument("--min-win-rate", type=float, default=0.5)
    parser.add_argument("--holding-bars", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--output",
        default="runtime/universe_candidates.csv",
        help="CSV report path",
    )
    parser.add_argument(
        "--write-env",
        action="store_true",
        help="write eligible top symbols to SYMBOLS in .env",
    )
    return parser


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
    config = UniverseSelectorConfig(
        exchanges=exchanges,
        min_exchanges=args.min_exchanges,
        days=args.days,
        timeframe=args.timeframe,
        max_history_pairs=args.history_pairs,
        min_quote_volume=args.min_volume,
        min_depth=args.min_depth,
        depth_band_bps=args.depth_band_bps,
        min_net_edge_bps=args.min_edge_bps,
        min_opportunities=args.min_opportunities,
        min_forward_win_rate=args.min_win_rate,
        holding_bars=args.holding_bars,
        concurrency=args.concurrency,
    )

    try:
        import ccxt.async_support as ccxt
    except ImportError as error:
        raise RuntimeError("install requirements.txt to select a universe") from error

    clients = {
        exchange: getattr(ccxt, exchange)(
            {"enableRateLimit": True, "options": {"defaultType": "spot"}}
        )
        for exchange in exchanges
    }
    try:
        await asyncio.gather(*(client.load_markets() for client in clients.values()))
        selector = UniverseSelector(
            clients=clients,
            config=config,
            fee_bps=settings.paper_fee_bps,
            slippage_bps=settings.paper_slippage_bps,
        )
        scores = await selector.select()
    finally:
        await asyncio.gather(
            *(client.close() for client in clients.values()),
            return_exceptions=True,
        )

    if not scores:
        raise RuntimeError("no pairs produced a historical score")
    output = Path(args.output)
    if not output.is_absolute():
        output = settings.project_root / output
    _write_report(output, scores)
    _print_ranking(scores, args.top)

    recommended = [score.symbol for score in scores if score.eligible][: args.top]
    print(f"\nCSV: {output}")
    if not recommended:
        print("SYMBOLS not recommended: no pair passed cost and convergence checks")
        return
    symbols_value = ",".join(recommended)
    print(f"Recommended: SYMBOLS={symbols_value}")
    if args.write_env:
        env_path = settings.project_root / ".env"
        set_key(str(env_path), "SYMBOLS", symbols_value)
        print(f"Updated: {env_path}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
