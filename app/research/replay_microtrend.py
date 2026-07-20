from __future__ import annotations

import argparse
import csv
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from itertools import product
from math import sqrt
from pathlib import Path
from statistics import mean, pstdev

from app.config.settings import AppSettings
from app.domain.models import ClosedTrade, Quote, Signal, SignalAction
from app.research.live_store import LiveQuoteStore, StoredFrame
from app.strategies.base import StrategyConfig
from app.strategies.micro_trend import MicroTrendStrategy
from app.trading.paper_trader import PaperTrader


@dataclass(frozen=True, slots=True)
class MicroTrendReplayParameters:
    fast_seconds: float
    slow_seconds: float
    entry_bps: float
    exit_bps: float
    stop_loss_bps: float
    take_profit_bps: float
    max_hold_seconds: int
    cooldown_seconds: float
    max_quote_age_seconds: float

    def __post_init__(self) -> None:
        if self.fast_seconds <= 0 or self.fast_seconds >= self.slow_seconds:
            raise ValueError("fast_seconds must be positive and less than slow_seconds")
        for name in (
            "entry_bps",
            "stop_loss_bps",
            "take_profit_bps",
            "max_hold_seconds",
            "cooldown_seconds",
            "max_quote_age_seconds",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True, slots=True)
class MicroTrendReplayMetrics:
    trade_count: int
    win_rate: float
    profit_factor: float | None
    total_pnl_bps: float
    expectancy_bps: float
    max_drawdown_bps: float
    average_hold_seconds: float
    sharpe: float
    score: float
    sample_adequate: bool
    candidate: bool


@dataclass(frozen=True, slots=True)
class MicroTrendReplayRun:
    session_id: str
    symbol: str
    exchange: str
    parameters: MicroTrendReplayParameters
    metrics: MicroTrendReplayMetrics
    trades: tuple[ClosedTrade, ...]

    def as_row(self) -> dict[str, str | int | float | bool | None]:
        return _report_row(
            row_type="pair",
            session_id=self.session_id,
            symbol=self.symbol,
            exchange=self.exchange,
            parameters=self.parameters,
            metrics=self.metrics,
        )


@dataclass(frozen=True, slots=True)
class MicroTrendReplaySummary:
    session_id: str
    parameters: MicroTrendReplayParameters
    metrics: MicroTrendReplayMetrics

    def as_row(self) -> dict[str, str | int | float | bool | None]:
        return _report_row(
            row_type="aggregate",
            session_id=self.session_id,
            symbol="ALL",
            exchange="ALL",
            parameters=self.parameters,
            metrics=self.metrics,
        )


def _report_row(
    *,
    row_type: str,
    session_id: str,
    symbol: str,
    exchange: str,
    parameters: MicroTrendReplayParameters,
    metrics: MicroTrendReplayMetrics,
) -> dict[str, str | int | float | bool | None]:
    return {
        "row_type": row_type,
        "session_id": session_id,
        "symbol": symbol,
        "exchange": exchange,
        **asdict(parameters),
        **asdict(metrics),
    }


def calculate_replay_metrics(
    trades: Sequence[ClosedTrade],
    *,
    min_trades: int,
    min_profit_factor: float,
) -> MicroTrendReplayMetrics:
    if not trades:
        return MicroTrendReplayMetrics(
            0,
            0,
            None,
            0,
            0,
            0,
            0,
            0,
            0,
            False,
            False,
        )

    returns_bps = [trade.return_fraction * 10_000 for trade in trades]
    wins = [value for value in returns_bps if value > 0]
    losses = [value for value in returns_bps if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss else None

    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in returns_bps:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)

    expectancy = mean(returns_bps)
    volatility = pstdev(returns_bps) if len(returns_bps) > 1 else 0.0
    sharpe = expectancy / volatility * sqrt(len(returns_bps)) if volatility else 0.0
    effective_profit_factor = (
        profit_factor if profit_factor is not None else (float("inf") if wins else 0.0)
    )
    # Keep losing grids below less-damaging alternatives instead of rewarding them
    # merely because a long losing streak makes drawdown and loss grow together.
    score = expectancy * sqrt(len(returns_bps))
    sample_adequate = len(trades) >= min_trades
    candidate = (
        sample_adequate
        and expectancy > 0
        and effective_profit_factor >= min_profit_factor
    )
    return MicroTrendReplayMetrics(
        trade_count=len(trades),
        win_rate=len(wins) / len(trades),
        profit_factor=profit_factor,
        total_pnl_bps=sum(returns_bps),
        expectancy_bps=expectancy,
        max_drawdown_bps=max_drawdown,
        average_hold_seconds=mean(trade.hold_seconds for trade in trades),
        sharpe=sharpe,
        score=score,
        sample_adequate=sample_adequate,
        candidate=candidate,
    )


def replay_symbol_exchange(
    *,
    session_id: str,
    symbol: str,
    exchange: str,
    frames: Sequence[StoredFrame],
    parameters: MicroTrendReplayParameters,
    fee_bps: Mapping[str, float],
    slippage_bps: float,
    notional: float,
    min_trades: int,
    min_profit_factor: float,
) -> MicroTrendReplayRun:
    if notional <= 0:
        raise ValueError("notional must be positive")
    if exchange not in fee_bps:
        raise ValueError(f"fee is missing for exchange {exchange!r}")

    strategy = MicroTrendStrategy(
        StrategyConfig(
            strategy_id="micro_trend_replay",
            symbols=(symbol,),
            exchanges=(exchange,),
            parameters={
                "fast_seconds": parameters.fast_seconds,
                "slow_seconds": parameters.slow_seconds,
                "entry_bps": parameters.entry_bps,
                "exit_bps": parameters.exit_bps,
                "stop_loss_bps": parameters.stop_loss_bps,
                "take_profit_bps": parameters.take_profit_bps,
                "max_hold_seconds": parameters.max_hold_seconds,
            },
        )
    )
    trader = PaperTrader(
        initial_equity=notional * 100,
        fee_bps=dict(fee_bps),
        slippage_bps=slippage_bps,
    )
    last_quote: Quote | None = None
    last_exit_at: datetime | None = None

    for index, frame in enumerate(frames):
        stored = frame.quotes.get(exchange)
        if stored is None:
            continue
        sampled_at = datetime.fromtimestamp(frame.sampled_at_ms / 1_000, tz=UTC)
        quote_age = max(0.0, (sampled_at - stored.received_at).total_seconds())
        if quote_age > parameters.max_quote_age_seconds:
            continue
        quote = Quote(
            exchange=exchange,
            symbol=symbol,
            bid=stored.bid,
            ask=stored.ask,
            occurred_at=stored.occurred_at,
            received_at=sampled_at,
        )
        last_quote = quote
        auto_closed = trader.mark_quote(quote)
        if auto_closed:
            last_exit_at = quote.received_at

        signals = strategy.on_quote(quote, trader.context)
        for signal in signals:
            if signal.action.is_entry:
                if index == len(frames) - 1:
                    continue
                if (
                    last_exit_at is not None
                    and (quote.received_at - last_exit_at).total_seconds()
                    < parameters.cooldown_seconds
                ):
                    continue
                trader.handle_signal(signal, quote, notional=notional)
                continue
            trade = trader.handle_signal(signal, quote)
            if trade is not None:
                last_exit_at = quote.received_at

    position = trader.context.find("micro_trend_replay", exchange, symbol)
    if position is not None and last_quote is not None:
        trader.handle_signal(
            Signal(
                strategy_id="micro_trend_replay",
                action=SignalAction.EXIT_LONG,
                exchange=exchange,
                symbol=symbol,
                reason="replay end of data",
                created_at=last_quote.received_at,
            ),
            last_quote,
        )

    trades = tuple(sorted(trader.closed_trades, key=lambda trade: trade.closed_at))
    return MicroTrendReplayRun(
        session_id=session_id,
        symbol=symbol,
        exchange=exchange,
        parameters=parameters,
        metrics=calculate_replay_metrics(
            trades,
            min_trades=min_trades,
            min_profit_factor=min_profit_factor,
        ),
        trades=trades,
    )


def build_parameter_grid(
    *,
    fast_seconds: Sequence[float],
    slow_seconds: Sequence[float],
    entry_bps: Sequence[float],
    exit_bps: float,
    stop_loss_bps: float,
    take_profit_bps: float,
    max_hold_seconds: int,
    cooldown_seconds: float,
    max_quote_age_seconds: float,
) -> list[MicroTrendReplayParameters]:
    grid = [
        MicroTrendReplayParameters(
            fast_seconds=fast,
            slow_seconds=slow,
            entry_bps=entry,
            exit_bps=exit_bps,
            stop_loss_bps=stop_loss_bps,
            take_profit_bps=take_profit_bps,
            max_hold_seconds=max_hold_seconds,
            cooldown_seconds=cooldown_seconds,
            max_quote_age_seconds=max_quote_age_seconds,
        )
        for fast, slow, entry in product(
            sorted(set(fast_seconds)),
            sorted(set(slow_seconds)),
            sorted(set(entry_bps)),
        )
        if 0 < fast < slow
    ]
    if not grid:
        raise ValueError("parameter grid is empty; every fast window must be below slow")
    return grid


def run_replay_grid(
    *,
    session_id: str,
    symbol_frames: Mapping[str, Sequence[StoredFrame]],
    exchanges: Sequence[str],
    parameter_grid: Sequence[MicroTrendReplayParameters],
    fee_bps: Mapping[str, float],
    slippage_bps: float,
    notional: float,
    min_trades: int,
    min_profit_factor: float,
) -> tuple[list[MicroTrendReplayRun], list[MicroTrendReplaySummary]]:
    runs = [
        replay_symbol_exchange(
            session_id=session_id,
            symbol=symbol,
            exchange=exchange,
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
        for exchange in exchanges
    ]
    summaries: list[MicroTrendReplaySummary] = []
    for parameters in parameter_grid:
        trades = sorted(
            (
                trade
                for run in runs
                if run.parameters == parameters
                for trade in run.trades
            ),
            key=lambda trade: trade.closed_at,
        )
        summaries.append(
            MicroTrendReplaySummary(
                session_id=session_id,
                parameters=parameters,
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
            item.metrics.expectancy_bps,
        ),
        reverse=True,
    )
    return runs, summaries


def write_replay_report(
    path: Path,
    *,
    runs: Sequence[MicroTrendReplayRun],
    summaries: Sequence[MicroTrendReplaySummary],
) -> None:
    rows = [summary.as_row() for summary in summaries]
    rows.extend(run.as_row() for run in runs)
    if not rows:
        raise ValueError("cannot write an empty replay report")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _profit_factor_label(value: float | None, trade_count: int) -> str:
    if value is None:
        return "inf" if trade_count else "-"
    return f"{value:.2f}"


def print_replay_rankings(
    *,
    runs: Sequence[MicroTrendReplayRun],
    summaries: Sequence[MicroTrendReplaySummary],
    top: int,
) -> None:
    print("\nMICRO-TREND REPLAY — PARAMETER RANKING")
    print(
        f"{'#':>2} {'fast/slow':>11} {'entry':>7} {'trades':>7} {'wins':>7} "
        f"{'PF':>6} {'expect':>9} {'pnl':>10} {'DD':>9} {'ok':>3}"
    )
    for index, item in enumerate(summaries[:top], start=1):
        params = item.parameters
        metrics = item.metrics
        print(
            f"{index:>2} {params.fast_seconds:g}/{params.slow_seconds:g}s "
            f"{params.entry_bps:>6.1f}b {metrics.trade_count:>7} "
            f"{metrics.win_rate:>6.1%} "
            f"{_profit_factor_label(metrics.profit_factor, metrics.trade_count):>6} "
            f"{metrics.expectancy_bps:>8.1f}b {metrics.total_pnl_bps:>9.1f}b "
            f"{metrics.max_drawdown_bps:>8.1f}b "
            f"{'yes' if metrics.candidate else 'no':>3}"
        )

    if not summaries:
        return
    best = summaries[0]
    matching = sorted(
        (run for run in runs if run.parameters == best.parameters),
        key=lambda run: (
            run.metrics.candidate,
            run.metrics.sample_adequate,
            run.metrics.trade_count > 0,
            run.metrics.score,
            run.metrics.expectancy_bps,
        ),
        reverse=True,
    )
    print(
        "\nPAIR / EXCHANGE RANKING FOR TOP AGGREGATE CONFIG "
        f"({best.parameters.fast_seconds:g}/{best.parameters.slow_seconds:g}s, "
        f"entry {best.parameters.entry_bps:g}b)"
    )
    print(
        f"{'#':>2} {'symbol':<14} {'exchange':<8} {'trades':>7} {'wins':>7} "
        f"{'PF':>6} {'expect':>9} {'pnl':>10} {'DD':>9}"
    )
    for index, run in enumerate(matching, start=1):
        metrics = run.metrics
        print(
            f"{index:>2} {run.symbol:<14} {run.exchange:<8} "
            f"{metrics.trade_count:>7} {metrics.win_rate:>6.1%} "
            f"{_profit_factor_label(metrics.profit_factor, metrics.trade_count):>6} "
            f"{metrics.expectancy_bps:>8.1f}b {metrics.total_pnl_bps:>9.1f}b "
            f"{metrics.max_drawdown_bps:>8.1f}b"
        )

    print()
    if best.metrics.candidate:
        print("Research candidate found; validate it on another independent session.")
    else:
        print("No configuration passed the minimum trade and profit-factor checks.")


def _float_values(raw: str) -> tuple[float, ...]:
    values = tuple(float(value.strip()) for value in raw.split(",") if value.strip())
    if not values:
        raise ValueError("expected at least one comma-separated number")
    return values


def _string_values(raw: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in raw.split(",") if value.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay time-based micro-trend configs on recorded bid/ask quotes"
    )
    parser.add_argument("--db", default="runtime/live_quotes.sqlite3")
    parser.add_argument("--session-id", help="default: latest recording session")
    parser.add_argument("--symbols", help="comma-separated symbols; default: all recorded")
    parser.add_argument("--exchanges", help="comma-separated exchanges; default: recorded")
    parser.add_argument("--fast-seconds", default="30,60,120")
    parser.add_argument("--slow-seconds", default="180,300,600")
    parser.add_argument("--entry-bps", default="6,12,20,30")
    parser.add_argument("--exit-bps", type=float, default=0)
    parser.add_argument("--stop-loss-bps", type=float, default=35)
    parser.add_argument("--take-profit-bps", type=float, default=70)
    parser.add_argument("--max-hold-seconds", type=int, default=900)
    parser.add_argument("--cooldown-seconds", type=float, default=20)
    parser.add_argument("--max-quote-age-seconds", type=float, default=3)
    parser.add_argument("--notional", type=float, default=100)
    parser.add_argument("--min-trades", type=int, default=20)
    parser.add_argument("--min-profit-factor", type=float, default=1.2)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--output", default="runtime/microtrend_replay.csv")
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
        session = (
            store.get_session(args.session_id) if args.session_id else sessions[0]
        )
        requested_symbols = (
            _string_values(args.symbols) if args.symbols else session.symbols
        )
        available = dict(store.iter_symbol_frames(session.session_id))

    missing_symbols = [symbol for symbol in requested_symbols if symbol not in available]
    if missing_symbols:
        raise ValueError("symbols are absent from the session: " + ", ".join(missing_symbols))
    symbol_frames = {symbol: available[symbol] for symbol in requested_symbols}
    exchanges = _string_values(args.exchanges) if args.exchanges else session.exchanges
    missing_fees = [
        exchange for exchange in exchanges if exchange not in settings.paper_fee_bps
    ]
    if missing_fees:
        raise ValueError("PAPER_FEE_BPS is missing: " + ", ".join(missing_fees))

    parameter_grid = build_parameter_grid(
        fast_seconds=_float_values(args.fast_seconds),
        slow_seconds=_float_values(args.slow_seconds),
        entry_bps=_float_values(args.entry_bps),
        exit_bps=args.exit_bps,
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
        parameter_grid=parameter_grid,
        fee_bps=settings.paper_fee_bps,
        slippage_bps=settings.paper_slippage_bps,
        notional=args.notional,
        min_trades=args.min_trades,
        min_profit_factor=args.min_profit_factor,
    )
    write_replay_report(output_path, runs=runs, summaries=summaries)
    duration_hours = (
        ((session.ended_at_ms or session.started_at_ms) - session.started_at_ms)
        / 3_600_000
    )
    print(
        f"Session: {session.session_id} | {duration_hours:.2f}h | "
        f"symbols={len(symbol_frames)} configs={len(parameter_grid)}"
    )
    print_replay_rankings(runs=runs, summaries=summaries, top=args.top)
    print(f"CSV: {output_path}")


def main() -> None:
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
