from __future__ import annotations

import argparse
import csv
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from statistics import mean

from app.config.settings import AppSettings
from app.domain.models import ClosedTrade, Quote, Signal, SignalAction
from app.research.live_store import LiveQuoteStore, StoredFrame, StoredQuote
from app.research.replay_microtrend import (
    MicroTrendReplayMetrics,
    calculate_replay_metrics,
)
from app.strategies.base import StrategyConfig
from app.strategies.confirmed_impulse import ConfirmedImpulseStrategy
from app.trading.paper_trader import PaperTrader


@dataclass(frozen=True, slots=True)
class ConfirmedImpulseReplayParameters:
    lookback_seconds: float
    min_confirmed_move_bps: float
    max_exchange_divergence_bps: float
    min_book_imbalance: float
    min_trade_flow_imbalance: float
    round_trip_cost_bps: float
    safety_margin_bps: float
    exit_momentum_bps: float
    exit_book_imbalance: float
    exit_trade_flow_imbalance: float
    stop_loss_bps: float
    take_profit_bps: float
    max_hold_seconds: int
    cooldown_seconds: float
    max_quote_age_seconds: float

    def __post_init__(self) -> None:
        if self.lookback_seconds <= 0:
            raise ValueError("lookback_seconds must be positive")
        if self.min_confirmed_move_bps < self.round_trip_cost_bps + self.safety_margin_bps:
            raise ValueError("confirmed move must cover round-trip cost and safety margin")
        if self.max_exchange_divergence_bps < 0:
            raise ValueError("max_exchange_divergence_bps cannot be negative")
        if self.round_trip_cost_bps < 0 or self.safety_margin_bps < 0:
            raise ValueError("cost and safety margin cannot be negative")
        if self.stop_loss_bps < 0 or self.take_profit_bps < 0:
            raise ValueError("stop and take profit cannot be negative")
        if self.max_hold_seconds <= 0 or self.max_quote_age_seconds < 0:
            raise ValueError("holding time must be positive and quote age non-negative")
        for value in (self.min_book_imbalance, self.min_trade_flow_imbalance):
            if not -1 <= value <= 1:
                raise ValueError("imbalance thresholds must be between -1 and 1")


@dataclass(frozen=True, slots=True)
class ConfirmedImpulseReplayRun:
    session_id: str
    symbol: str
    parameters: ConfirmedImpulseReplayParameters
    feature_coverage: float
    metrics: MicroTrendReplayMetrics
    trades: tuple[ClosedTrade, ...]

    def as_row(self) -> dict[str, str | int | float | bool | None]:
        return _report_row(
            row_type="pair",
            session_id=self.session_id,
            symbol=self.symbol,
            parameters=self.parameters,
            feature_coverage=self.feature_coverage,
            metrics=self.metrics,
        )


@dataclass(frozen=True, slots=True)
class ConfirmedImpulseReplaySummary:
    session_id: str
    parameters: ConfirmedImpulseReplayParameters
    feature_coverage: float
    metrics: MicroTrendReplayMetrics

    def as_row(self) -> dict[str, str | int | float | bool | None]:
        return _report_row(
            row_type="aggregate",
            session_id=self.session_id,
            symbol="ALL",
            parameters=self.parameters,
            feature_coverage=self.feature_coverage,
            metrics=self.metrics,
        )


def _report_row(
    *,
    row_type: str,
    session_id: str,
    symbol: str,
    parameters: ConfirmedImpulseReplayParameters,
    feature_coverage: float,
    metrics: MicroTrendReplayMetrics,
) -> dict[str, str | int | float | bool | None]:
    return {
        "row_type": row_type,
        "session_id": session_id,
        "symbol": symbol,
        **asdict(parameters),
        "feature_coverage": feature_coverage,
        **asdict(metrics),
    }


def _has_features(quote: StoredQuote) -> bool:
    values = (
        quote.bid_size,
        quote.ask_size,
        quote.buy_volume,
        quote.sell_volume,
        quote.trade_flow_window_seconds,
    )
    if any(value is None for value in values):
        return False
    return bool(
        (quote.bid_size or 0) + (quote.ask_size or 0) > 0
        and (quote.buy_volume or 0) + (quote.sell_volume or 0) > 0
    )


def feature_coverage(
    frames: Sequence[StoredFrame],
    exchanges: Sequence[str],
) -> float:
    total = len(frames) * len(exchanges)
    if total == 0:
        return 0.0
    available = sum(
        _has_features(frame.quotes[exchange])
        for frame in frames
        for exchange in exchanges
        if exchange in frame.quotes
    )
    return available / total


def _to_quote(
    stored: StoredQuote,
    *,
    sampled_at: datetime,
) -> Quote:
    return Quote(
        exchange=stored.exchange,
        symbol=stored.symbol,
        bid=stored.bid,
        ask=stored.ask,
        occurred_at=stored.occurred_at,
        received_at=sampled_at,
        bid_size=stored.bid_size,
        ask_size=stored.ask_size,
        buy_volume=stored.buy_volume,
        sell_volume=stored.sell_volume,
        trade_flow_window_seconds=stored.trade_flow_window_seconds,
    )


def replay_confirmed_impulse_symbol(
    *,
    session_id: str,
    symbol: str,
    exchanges: Sequence[str],
    frames: Sequence[StoredFrame],
    parameters: ConfirmedImpulseReplayParameters,
    fee_bps: Mapping[str, float],
    slippage_bps: float,
    notional: float,
    min_trades: int,
    min_profit_factor: float,
) -> ConfirmedImpulseReplayRun:
    if notional <= 0:
        raise ValueError("notional must be positive")
    missing_fees = [exchange for exchange in exchanges if exchange not in fee_bps]
    if missing_fees:
        raise ValueError("fees are missing for: " + ", ".join(missing_fees))
    strategy = ConfirmedImpulseStrategy(
        StrategyConfig(
            strategy_id="confirmed_impulse_replay",
            symbols=(symbol,),
            exchanges=tuple(exchanges),
            parameters={
                key: value
                for key, value in asdict(parameters).items()
                if key != "cooldown_seconds"
            },
        )
    )
    trader = PaperTrader(
        initial_equity=notional * 100,
        fee_bps=dict(fee_bps),
        slippage_bps=slippage_bps,
    )
    latest_quotes: dict[tuple[str, str], Quote] = {}
    last_exit_at: datetime | None = None

    for frame in frames:
        sampled_at = datetime.fromtimestamp(frame.sampled_at_ms / 1_000, tz=UTC)
        for exchange in exchanges:
            stored = frame.quotes.get(exchange)
            if stored is None or not _has_features(stored):
                continue
            quote_age = max(0.0, (sampled_at - stored.received_at).total_seconds())
            if quote_age > parameters.max_quote_age_seconds:
                continue
            quote = _to_quote(stored, sampled_at=sampled_at)
            latest_quotes[(exchange, symbol)] = quote
            if trader.mark_quote(quote):
                last_exit_at = sampled_at

            for signal in strategy.on_quote(quote, trader.context):
                execution_quote = latest_quotes.get((signal.exchange, signal.symbol))
                if execution_quote is None:
                    continue
                if signal.action.is_entry:
                    if (
                        last_exit_at is not None
                        and (sampled_at - last_exit_at).total_seconds()
                        < parameters.cooldown_seconds
                    ):
                        continue
                    trader.handle_signal(signal, execution_quote, notional=notional)
                    continue
                trade = trader.handle_signal(signal, execution_quote)
                if trade is not None:
                    last_exit_at = sampled_at

    for position in tuple(trader.positions.values()):
        quote = latest_quotes.get((position.exchange, position.symbol))
        if quote is None:
            continue
        trader.handle_signal(
            Signal(
                strategy_id=position.strategy_id,
                action=SignalAction.EXIT_LONG,
                exchange=position.exchange,
                symbol=position.symbol,
                reason="replay end of data",
                created_at=quote.received_at,
            ),
            quote,
        )

    trades = tuple(sorted(trader.closed_trades, key=lambda trade: trade.closed_at))
    return ConfirmedImpulseReplayRun(
        session_id=session_id,
        symbol=symbol,
        parameters=parameters,
        feature_coverage=feature_coverage(frames, exchanges),
        metrics=calculate_replay_metrics(
            trades,
            min_trades=min_trades,
            min_profit_factor=min_profit_factor,
        ),
        trades=trades,
    )


def build_parameter_grid(
    *,
    lookback_seconds: Sequence[float],
    confirmed_move_bps: Sequence[float],
    book_imbalance: Sequence[float],
    trade_flow_imbalance: Sequence[float],
    max_exchange_divergence_bps: float,
    round_trip_cost_bps: float,
    safety_margin_bps: float,
    exit_momentum_bps: float,
    exit_book_imbalance: float,
    exit_trade_flow_imbalance: float,
    stop_loss_bps: float,
    take_profit_bps: float,
    max_hold_seconds: int,
    cooldown_seconds: float,
    max_quote_age_seconds: float,
) -> list[ConfirmedImpulseReplayParameters]:
    cost_floor = round_trip_cost_bps + safety_margin_bps
    grid = [
        ConfirmedImpulseReplayParameters(
            lookback_seconds=lookback,
            min_confirmed_move_bps=move,
            max_exchange_divergence_bps=max_exchange_divergence_bps,
            min_book_imbalance=book,
            min_trade_flow_imbalance=flow,
            round_trip_cost_bps=round_trip_cost_bps,
            safety_margin_bps=safety_margin_bps,
            exit_momentum_bps=exit_momentum_bps,
            exit_book_imbalance=exit_book_imbalance,
            exit_trade_flow_imbalance=exit_trade_flow_imbalance,
            stop_loss_bps=stop_loss_bps,
            take_profit_bps=take_profit_bps,
            max_hold_seconds=max_hold_seconds,
            cooldown_seconds=cooldown_seconds,
            max_quote_age_seconds=max_quote_age_seconds,
        )
        for lookback, move, book, flow in product(
            sorted(set(lookback_seconds)),
            sorted(set(confirmed_move_bps)),
            sorted(set(book_imbalance)),
            sorted(set(trade_flow_imbalance)),
        )
        if move >= cost_floor
    ]
    if not grid:
        raise ValueError("parameter grid has no move above the configured cost floor")
    return grid


def run_replay_grid(
    *,
    session_id: str,
    symbol_frames: Mapping[str, Sequence[StoredFrame]],
    exchanges: Sequence[str],
    parameter_grid: Sequence[ConfirmedImpulseReplayParameters],
    fee_bps: Mapping[str, float],
    slippage_bps: float,
    notional: float,
    min_trades: int,
    min_profit_factor: float,
) -> tuple[list[ConfirmedImpulseReplayRun], list[ConfirmedImpulseReplaySummary]]:
    runs = [
        replay_confirmed_impulse_symbol(
            session_id=session_id,
            symbol=symbol,
            exchanges=exchanges,
            frames=frames,
            parameters=parameters,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            notional=notional,
            min_trades=min_trades,
            min_profit_factor=min_profit_factor,
        )
        for parameters in parameter_grid
        for symbol, frames in symbol_frames.items()
    ]
    summaries: list[ConfirmedImpulseReplaySummary] = []
    for parameters in parameter_grid:
        matching = [run for run in runs if run.parameters == parameters]
        trades = sorted(
            (trade for run in matching for trade in run.trades),
            key=lambda trade: trade.closed_at,
        )
        summaries.append(
            ConfirmedImpulseReplaySummary(
                session_id=session_id,
                parameters=parameters,
                feature_coverage=(
                    mean(run.feature_coverage for run in matching) if matching else 0.0
                ),
                metrics=calculate_replay_metrics(
                    trades,
                    min_trades=min_trades,
                    min_profit_factor=min_profit_factor,
                ),
            )
        )
    summaries.sort(
        key=lambda item: (
            item.metrics.candidate,
            item.metrics.sample_adequate,
            item.metrics.trade_count > 0,
            item.metrics.score,
        ),
        reverse=True,
    )
    return runs, summaries


def write_report(
    path: Path,
    *,
    runs: Sequence[ConfirmedImpulseReplayRun],
    summaries: Sequence[ConfirmedImpulseReplaySummary],
) -> None:
    rows = [summary.as_row() for summary in summaries]
    rows.extend(run.as_row() for run in runs)
    if not rows:
        raise ValueError("cannot write an empty confirmed impulse report")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _pf(value: float | None, trades: int) -> str:
    if value is None:
        return "inf" if trades else "-"
    return f"{value:.2f}"


def print_rankings(
    *,
    runs: Sequence[ConfirmedImpulseReplayRun],
    summaries: Sequence[ConfirmedImpulseReplaySummary],
    top: int,
) -> None:
    print("\nCONFIRMED IMPULSE REPLAY")
    print(
        f"{'#':>2} {'look':>6} {'move':>7} {'book':>6} {'flow':>6} "
        f"{'trades':>7} {'wins':>7} {'PF':>6} {'expect':>9} {'pnl':>10} {'ok':>3}"
    )
    for index, summary in enumerate(summaries[:top], start=1):
        params = summary.parameters
        metrics = summary.metrics
        print(
            f"{index:>2} {params.lookback_seconds:>5g}s "
            f"{params.min_confirmed_move_bps:>6.1f}b "
            f"{params.min_book_imbalance:>+5.2f} "
            f"{params.min_trade_flow_imbalance:>+5.2f} "
            f"{metrics.trade_count:>7} {metrics.win_rate:>6.1%} "
            f"{_pf(metrics.profit_factor, metrics.trade_count):>6} "
            f"{metrics.expectancy_bps:>8.1f}b {metrics.total_pnl_bps:>9.1f}b "
            f"{'yes' if metrics.candidate else 'no':>3}"
        )
    if not summaries:
        return
    best = summaries[0]
    matching = sorted(
        (run for run in runs if run.parameters == best.parameters),
        key=lambda run: (run.metrics.sample_adequate, run.metrics.score),
        reverse=True,
    )
    print("\nPAIR RANKING FOR TOP CONFIG")
    print(
        f"{'#':>2} {'symbol':<14} {'features':>9} {'trades':>7} "
        f"{'wins':>7} {'PF':>6} {'expect':>9} {'pnl':>10}"
    )
    for index, run in enumerate(matching, start=1):
        metrics = run.metrics
        print(
            f"{index:>2} {run.symbol:<14} {run.feature_coverage:>8.1%} "
            f"{metrics.trade_count:>7} {metrics.win_rate:>6.1%} "
            f"{_pf(metrics.profit_factor, metrics.trade_count):>6} "
            f"{metrics.expectancy_bps:>8.1f}b {metrics.total_pnl_bps:>9.1f}b"
        )


def _floats(raw: str) -> tuple[float, ...]:
    values = tuple(float(value.strip()) for value in raw.split(",") if value.strip())
    if not values:
        raise ValueError("expected at least one comma-separated number")
    return values


def _strings(raw: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in raw.split(",") if value.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay cost-aware cross-exchange impulse on feature-rich quotes"
    )
    parser.add_argument("--db", default="runtime/live_quotes.sqlite3")
    parser.add_argument("--session-id", help="default: latest recording session")
    parser.add_argument("--symbols", help="comma-separated symbols; default: all recorded")
    parser.add_argument("--exchanges", help="default: exchanges recorded in the session")
    parser.add_argument("--lookback-seconds", default="30,60,120")
    parser.add_argument("--confirmed-move-bps", default="30,40,60")
    parser.add_argument("--book-imbalance", default="0.10,0.20")
    parser.add_argument("--trade-flow-imbalance", default="0.05,0.15")
    parser.add_argument("--max-exchange-divergence-bps", type=float, default=10)
    parser.add_argument("--round-trip-cost-bps", type=float, default=24)
    parser.add_argument("--safety-margin-bps", type=float, default=6)
    parser.add_argument("--exit-momentum-bps", type=float, default=5)
    parser.add_argument("--exit-book-imbalance", type=float, default=-0.05)
    parser.add_argument("--exit-trade-flow-imbalance", type=float, default=-0.05)
    parser.add_argument("--stop-loss-bps", type=float, default=60)
    parser.add_argument("--take-profit-bps", type=float, default=120)
    parser.add_argument("--max-hold-seconds", type=int, default=300)
    parser.add_argument("--cooldown-seconds", type=float, default=20)
    parser.add_argument("--max-quote-age-seconds", type=float, default=3)
    parser.add_argument("--notional", type=float, default=100)
    parser.add_argument("--min-trades", type=int, default=20)
    parser.add_argument("--min-profit-factor", type=float, default=1.2)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--output", default="runtime/confirmed_impulse_replay.csv")
    return parser


def run(args: argparse.Namespace) -> None:
    for name in ("notional", "min_trades", "min_profit_factor", "top"):
        if getattr(args, name) <= 0:
            raise ValueError(f"{name} must be positive")
    settings = AppSettings.from_env()
    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = settings.project_root / db_path
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = settings.project_root / output_path

    with LiveQuoteStore(db_path, read_only=True) as store:
        sessions = store.list_sessions()
        if not sessions:
            raise RuntimeError("the live quote database has no recording sessions")
        session = store.get_session(args.session_id) if args.session_id else sessions[0]
        requested_symbols = _strings(args.symbols) if args.symbols else session.symbols
        available = dict(store.iter_symbol_frames(session.session_id))

    missing = [symbol for symbol in requested_symbols if symbol not in available]
    if missing:
        raise ValueError("symbols are absent from the session: " + ", ".join(missing))
    symbol_frames = {symbol: available[symbol] for symbol in requested_symbols}
    exchanges = _strings(args.exchanges) if args.exchanges else session.exchanges
    if max(
        (feature_coverage(frames, exchanges) for frames in symbol_frames.values()),
        default=0.0,
    ) == 0:
        raise RuntimeError(
            "this session has no order-book/trade-flow features; record a new session"
        )

    grid = build_parameter_grid(
        lookback_seconds=_floats(args.lookback_seconds),
        confirmed_move_bps=_floats(args.confirmed_move_bps),
        book_imbalance=_floats(args.book_imbalance),
        trade_flow_imbalance=_floats(args.trade_flow_imbalance),
        max_exchange_divergence_bps=args.max_exchange_divergence_bps,
        round_trip_cost_bps=args.round_trip_cost_bps,
        safety_margin_bps=args.safety_margin_bps,
        exit_momentum_bps=args.exit_momentum_bps,
        exit_book_imbalance=args.exit_book_imbalance,
        exit_trade_flow_imbalance=args.exit_trade_flow_imbalance,
        stop_loss_bps=args.stop_loss_bps,
        take_profit_bps=args.take_profit_bps,
        max_hold_seconds=args.max_hold_seconds,
        cooldown_seconds=args.cooldown_seconds,
        max_quote_age_seconds=args.max_quote_age_seconds,
    )
    runs, summaries = run_replay_grid(
        session_id=session.session_id,
        symbol_frames=symbol_frames,
        exchanges=exchanges,
        parameter_grid=grid,
        fee_bps=settings.paper_fee_bps,
        slippage_bps=settings.paper_slippage_bps,
        notional=args.notional,
        min_trades=args.min_trades,
        min_profit_factor=args.min_profit_factor,
    )
    write_report(output_path, runs=runs, summaries=summaries)
    print(
        f"Session: {session.session_id} | symbols={len(symbol_frames)} "
        f"configs={len(grid)}"
    )
    print_rankings(runs=runs, summaries=summaries, top=args.top)
    print(f"\nCSV: {output_path}")


def main() -> None:
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
