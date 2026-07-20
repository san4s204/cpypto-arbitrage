from __future__ import annotations

import argparse
import csv
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from itertools import product
from pathlib import Path

from app.config.settings import AppSettings
from app.domain.models import ClosedTrade, Quote, Signal, SignalAction
from app.research.live_store import LiveQuoteStore, StoredFrame, StoredQuote
from app.research.replay_microtrend import (
    MicroTrendReplayMetrics,
    calculate_replay_metrics,
)
from app.trading.paper_trader import PaperTrader


@dataclass(frozen=True, slots=True)
class LatencyReplayParameters:
    lookback_seconds: float
    leader_move_bps: float
    min_gap_bps: float
    response_bps: float
    stop_move_bps: float
    max_hold_seconds: float
    cooldown_seconds: float
    max_quote_age_seconds: float

    def __post_init__(self) -> None:
        for name in (
            "lookback_seconds",
            "leader_move_bps",
            "response_bps",
            "stop_move_bps",
            "max_hold_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("min_gap_bps", "cooldown_seconds", "max_quote_age_seconds"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True, slots=True)
class LatencyReplayRun:
    session_id: str
    symbol: str
    parameters: LatencyReplayParameters
    metrics: MicroTrendReplayMetrics
    trades: tuple[ClosedTrade, ...]

    def as_row(self) -> dict[str, str | int | float | bool | None]:
        return _report_row(
            row_type="pair",
            session_id=self.session_id,
            symbol=self.symbol,
            parameters=self.parameters,
            metrics=self.metrics,
        )


@dataclass(frozen=True, slots=True)
class LatencyReplaySummary:
    session_id: str
    parameters: LatencyReplayParameters
    metrics: MicroTrendReplayMetrics

    def as_row(self) -> dict[str, str | int | float | bool | None]:
        return _report_row(
            row_type="aggregate",
            session_id=self.session_id,
            symbol="ALL",
            parameters=self.parameters,
            metrics=self.metrics,
        )


def _report_row(
    *,
    row_type: str,
    session_id: str,
    symbol: str,
    parameters: LatencyReplayParameters,
    metrics: MicroTrendReplayMetrics,
) -> dict[str, str | int | float | bool | None]:
    return {
        "row_type": row_type,
        "session_id": session_id,
        "symbol": symbol,
        **asdict(parameters),
        **asdict(metrics),
    }


def _mid(quote: StoredQuote) -> float:
    return (quote.bid + quote.ask) / 2


def _is_fresh(
    frame: StoredFrame,
    quote: StoredQuote,
    *,
    max_quote_age_seconds: float,
) -> bool:
    sampled_at = datetime.fromtimestamp(frame.sampled_at_ms / 1_000, tz=UTC)
    return max(0.0, (sampled_at - quote.received_at).total_seconds()) <= (
        max_quote_age_seconds
    )


def _to_quote(stored: StoredQuote, frame: StoredFrame) -> Quote:
    sampled_at = datetime.fromtimestamp(frame.sampled_at_ms / 1_000, tz=UTC)
    return Quote(
        exchange=stored.exchange,
        symbol=stored.symbol,
        bid=stored.bid,
        ask=stored.ask,
        occurred_at=stored.occurred_at,
        received_at=sampled_at,
    )


def _exit_action(entry_action: SignalAction) -> SignalAction:
    return (
        SignalAction.EXIT_LONG
        if entry_action is SignalAction.ENTER_LONG
        else SignalAction.EXIT_SHORT
    )


def replay_latency_symbol(
    *,
    session_id: str,
    symbol: str,
    exchanges: Sequence[str],
    frames: Sequence[StoredFrame],
    parameters: LatencyReplayParameters,
    fee_bps: Mapping[str, float],
    slippage_bps: float,
    notional: float,
    min_trades: int,
    min_profit_factor: float,
    max_gap_seconds: float = 15,
) -> LatencyReplayRun:
    if notional <= 0:
        raise ValueError("notional must be positive")
    if slippage_bps < 0:
        raise ValueError("slippage_bps cannot be negative")
    if max_gap_seconds <= 0:
        raise ValueError("max_gap_seconds must be positive")
    missing_fees = [exchange for exchange in exchanges if exchange not in fee_bps]
    if missing_fees:
        raise ValueError("fees are missing for: " + ", ".join(missing_fees))

    trader = PaperTrader(
        initial_equity=notional * 100,
        fee_bps=dict(fee_bps),
        slippage_bps=slippage_bps,
    )
    timestamps = [frame.sampled_at_ms for frame in frames]
    lookback_ms = int(parameters.lookback_seconds * 1_000)
    max_gap_ms = int(max_gap_seconds * 1_000)
    last_exit_ms: int | None = None
    index = 1

    while index < len(frames):
        current = frames[index]
        if (
            last_exit_ms is not None
            and current.sampled_at_ms - last_exit_ms
            < parameters.cooldown_seconds * 1_000
        ):
            index += 1
            continue

        target_ms = current.sampled_at_ms - lookback_ms
        previous_index = bisect_right(timestamps, target_ms, hi=index) - 1
        if previous_index < 0:
            index += 1
            continue
        previous = frames[previous_index]
        actual_lookback_ms = current.sampled_at_ms - previous.sampled_at_ms
        if actual_lookback_ms > lookback_ms + max_gap_ms:
            index += 1
            continue

        common_exchanges = [
            exchange
            for exchange in exchanges
            if exchange in previous.quotes
            and exchange in current.quotes
            and _is_fresh(
                previous,
                previous.quotes[exchange],
                max_quote_age_seconds=parameters.max_quote_age_seconds,
            )
            and _is_fresh(
                current,
                current.quotes[exchange],
                max_quote_age_seconds=parameters.max_quote_age_seconds,
            )
        ]
        if len(common_exchanges) < 2:
            index += 1
            continue

        returns = {
            exchange: (
                _mid(current.quotes[exchange]) / _mid(previous.quotes[exchange]) - 1
            )
            * 10_000
            for exchange in common_exchanges
        }
        leader_exchange, leader_move = max(
            returns.items(),
            key=lambda item: abs(item[1]),
        )
        follower_exchange, follower_move = min(
            (
                (exchange, move)
                for exchange, move in returns.items()
                if exchange != leader_exchange
            ),
            key=lambda item: abs(item[1]),
        )
        direction = 1 if leader_move > 0 else -1
        directional_gap = direction * (leader_move - follower_move)
        if (
            abs(leader_move) < parameters.leader_move_bps
            or directional_gap < parameters.min_gap_bps
        ):
            index += 1
            continue

        entry_stored = current.quotes[follower_exchange]
        entry_quote = _to_quote(entry_stored, current)
        entry_action = (
            SignalAction.ENTER_LONG if direction > 0 else SignalAction.ENTER_SHORT
        )
        trader.handle_signal(
            Signal(
                strategy_id="latency_replay",
                action=entry_action,
                exchange=follower_exchange,
                symbol=symbol,
                reason=(
                    f"{leader_exchange} moved {leader_move:.1f} bps while "
                    f"{follower_exchange} moved {follower_move:.1f} bps"
                ),
                created_at=entry_quote.received_at,
                metadata={"group_id": f"{leader_exchange}->{follower_exchange}"},
            ),
            entry_quote,
            notional=notional,
        )

        follower_start = _mid(entry_stored)
        exit_frame = current
        exit_stored = entry_stored
        exit_reason = "replay end of data"
        scan = index + 1
        while scan < len(frames):
            candidate = frames[scan]
            if candidate.sampled_at_ms - frames[scan - 1].sampled_at_ms > max_gap_ms:
                exit_reason = "recording gap"
                break
            stored = candidate.quotes.get(follower_exchange)
            if stored is None or not _is_fresh(
                candidate,
                stored,
                max_quote_age_seconds=parameters.max_quote_age_seconds,
            ):
                scan += 1
                continue
            exit_frame = candidate
            exit_stored = stored
            elapsed_seconds = (
                candidate.sampled_at_ms - current.sampled_at_ms
            ) / 1_000
            follower_response = direction * (_mid(stored) / follower_start - 1) * 10_000
            if follower_response >= parameters.response_bps:
                exit_reason = "follower response target"
                break
            if follower_response <= -parameters.stop_move_bps:
                exit_reason = "adverse follower move"
                break
            if elapsed_seconds >= parameters.max_hold_seconds:
                exit_reason = "maximum holding time"
                break
            scan += 1

        exit_quote = _to_quote(exit_stored, exit_frame)
        trade = trader.handle_signal(
            Signal(
                strategy_id="latency_replay",
                action=_exit_action(entry_action),
                exchange=follower_exchange,
                symbol=symbol,
                reason=exit_reason,
                created_at=exit_quote.received_at,
            ),
            exit_quote,
        )
        if trade is None:
            raise RuntimeError("latency replay failed to close an open position")
        last_exit_ms = exit_frame.sampled_at_ms
        index = max(scan, index + 1) + 1

    trades = tuple(sorted(trader.closed_trades, key=lambda trade: trade.closed_at))
    return LatencyReplayRun(
        session_id=session_id,
        symbol=symbol,
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
    lookback_seconds: Sequence[float],
    leader_move_bps: Sequence[float],
    min_gap_bps: Sequence[float],
    response_bps: Sequence[float],
    stop_move_bps: float,
    max_hold_seconds: Sequence[float],
    cooldown_seconds: float,
    max_quote_age_seconds: float,
) -> list[LatencyReplayParameters]:
    grid = [
        LatencyReplayParameters(
            lookback_seconds=lookback,
            leader_move_bps=leader,
            min_gap_bps=gap,
            response_bps=response,
            stop_move_bps=stop_move_bps,
            max_hold_seconds=max_hold,
            cooldown_seconds=cooldown_seconds,
            max_quote_age_seconds=max_quote_age_seconds,
        )
        for lookback, leader, gap, response, max_hold in product(
            sorted(set(lookback_seconds)),
            sorted(set(leader_move_bps)),
            sorted(set(min_gap_bps)),
            sorted(set(response_bps)),
            sorted(set(max_hold_seconds)),
        )
    ]
    if not grid:
        raise ValueError("parameter grid is empty")
    return grid


def run_replay_grid(
    *,
    session_id: str,
    symbol_frames: Mapping[str, Sequence[StoredFrame]],
    exchanges: Sequence[str],
    parameter_grid: Sequence[LatencyReplayParameters],
    fee_bps: Mapping[str, float],
    slippage_bps: float,
    notional: float,
    min_trades: int,
    min_profit_factor: float,
    max_gap_seconds: float,
) -> tuple[list[LatencyReplayRun], list[LatencyReplaySummary]]:
    runs = [
        replay_latency_symbol(
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
            max_gap_seconds=max_gap_seconds,
        )
        for parameters in parameter_grid
        for symbol, frames in symbol_frames.items()
    ]
    summaries: list[LatencyReplaySummary] = []
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
            LatencyReplaySummary(
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
    runs: Sequence[LatencyReplayRun],
    summaries: Sequence[LatencyReplaySummary],
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
    runs: Sequence[LatencyReplayRun],
    summaries: Sequence[LatencyReplaySummary],
    top: int,
) -> None:
    print("\nCOST-AWARE LATENCY REPLAY")
    print(
        f"{'#':>2} {'look':>5} {'move':>6} {'gap':>6} {'resp':>6} {'hold':>6} "
        f"{'trades':>7} {'wins':>7} {'PF':>6} {'expect':>9} {'pnl':>10} {'ok':>3}"
    )
    for position, item in enumerate(summaries[:top], start=1):
        params = item.parameters
        metrics = item.metrics
        print(
            f"{position:>2} {params.lookback_seconds:>4g}s "
            f"{params.leader_move_bps:>5.1f}b {params.min_gap_bps:>5.1f}b "
            f"{params.response_bps:>5.1f}b {params.max_hold_seconds:>5g}s "
            f"{metrics.trade_count:>7} {metrics.win_rate:>6.1%} "
            f"{_profit_factor_label(metrics.profit_factor, metrics.trade_count):>6} "
            f"{metrics.expectancy_bps:>8.1f}b {metrics.total_pnl_bps:>9.1f}b "
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
    print("\nPAIR RANKING FOR TOP CONFIG")
    print(
        f"{'#':>2} {'symbol':<14} {'trades':>7} {'wins':>7} "
        f"{'PF':>6} {'expect':>9} {'pnl':>10}"
    )
    for position, run in enumerate(matching, start=1):
        metrics = run.metrics
        print(
            f"{position:>2} {run.symbol:<14} {metrics.trade_count:>7} "
            f"{metrics.win_rate:>6.1%} "
            f"{_profit_factor_label(metrics.profit_factor, metrics.trade_count):>6} "
            f"{metrics.expectancy_bps:>8.1f}b {metrics.total_pnl_bps:>9.1f}b"
        )

    print()
    if best.metrics.candidate:
        print("Research candidate found; validate this exact config on another session.")
    else:
        print("No configuration passed the minimum trade and profit-factor checks.")


def _float_values(raw: str) -> tuple[float, ...]:
    values = tuple(float(value.strip()) for value in raw.split(",") if value.strip())
    if not values:
        raise ValueError("expected at least one comma-separated number")
    return values


def _string_values(raw: str) -> tuple[str, ...]:
    values = tuple(value.strip() for value in raw.split(",") if value.strip())
    if not values:
        raise ValueError("expected at least one comma-separated value")
    return values


def _fee_values(raw: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for item in raw.split(","):
        exchange, value = item.split(":", maxsplit=1)
        result[exchange.strip()] = float(value)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay executable lead-lag trades on synchronized bid/ask quotes"
    )
    parser.add_argument("--db", default="runtime/live_quotes.sqlite3")
    parser.add_argument("--session-id", help="default: latest recording session")
    parser.add_argument("--symbols", help="comma-separated symbols; default: all recorded")
    parser.add_argument("--exchanges", help="comma-separated exchanges; default: recorded")
    parser.add_argument("--lookback-seconds", default="5")
    parser.add_argument("--leader-move-bps", default="12,20,30")
    parser.add_argument("--min-gap-bps", default="8,15,25")
    parser.add_argument("--response-bps", default="8,12,20,30,40")
    parser.add_argument("--stop-move-bps", type=float, default=30)
    parser.add_argument("--max-hold-seconds", default="5,10,20,30")
    parser.add_argument("--cooldown-seconds", type=float, default=5)
    parser.add_argument("--max-quote-age-seconds", type=float, default=3)
    parser.add_argument("--max-gap-seconds", type=float, default=15)
    parser.add_argument("--notional", type=float, default=100)
    parser.add_argument("--min-trades", type=int, default=10)
    parser.add_argument("--min-profit-factor", type=float, default=1.2)
    parser.add_argument("--fee-bps", help="override, e.g. bybit:10,okx:10")
    parser.add_argument("--slippage-bps", type=float)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--output", default="runtime/latency_replay.csv")
    return parser


def run(args: argparse.Namespace) -> None:
    for name in ("notional", "min_trades", "min_profit_factor", "top"):
        if getattr(args, name) <= 0:
            raise ValueError(f"{name} must be positive")
    if args.max_gap_seconds <= 0:
        raise ValueError("max_gap_seconds must be positive")

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
        requested_symbols = (
            _string_values(args.symbols) if args.symbols else session.symbols
        )
        available = dict(store.iter_symbol_frames(session.session_id))

    missing_symbols = [symbol for symbol in requested_symbols if symbol not in available]
    if missing_symbols:
        raise ValueError("symbols are absent from the session: " + ", ".join(missing_symbols))
    symbol_frames = {symbol: available[symbol] for symbol in requested_symbols}
    exchanges = _string_values(args.exchanges) if args.exchanges else session.exchanges
    fees = _fee_values(args.fee_bps) if args.fee_bps else settings.paper_fee_bps
    missing_fees = [exchange for exchange in exchanges if exchange not in fees]
    if missing_fees:
        raise ValueError("fees are missing for: " + ", ".join(missing_fees))
    slippage_bps = (
        args.slippage_bps
        if args.slippage_bps is not None
        else settings.paper_slippage_bps
    )

    parameter_grid = build_parameter_grid(
        lookback_seconds=_float_values(args.lookback_seconds),
        leader_move_bps=_float_values(args.leader_move_bps),
        min_gap_bps=_float_values(args.min_gap_bps),
        response_bps=_float_values(args.response_bps),
        stop_move_bps=args.stop_move_bps,
        max_hold_seconds=_float_values(args.max_hold_seconds),
        cooldown_seconds=args.cooldown_seconds,
        max_quote_age_seconds=args.max_quote_age_seconds,
    )
    runs, summaries = run_replay_grid(
        session_id=session.session_id,
        symbol_frames=symbol_frames,
        exchanges=exchanges,
        parameter_grid=parameter_grid,
        fee_bps=fees,
        slippage_bps=slippage_bps,
        notional=args.notional,
        min_trades=args.min_trades,
        min_profit_factor=args.min_profit_factor,
        max_gap_seconds=args.max_gap_seconds,
    )
    write_replay_report(output_path, runs=runs, summaries=summaries)
    duration_hours = (
        ((session.ended_at_ms or session.started_at_ms) - session.started_at_ms)
        / 3_600_000
    )
    fee_label = ",".join(f"{exchange}:{fees[exchange]:g}" for exchange in exchanges)
    print(
        f"Session: {session.session_id} | {duration_hours:.2f}h | "
        f"symbols={len(symbol_frames)} configs={len(parameter_grid)} | "
        f"fees={fee_label} slippage={slippage_bps:g}b"
    )
    print_replay_rankings(runs=runs, summaries=summaries, top=args.top)
    print(f"CSV: {output_path}")


def main() -> None:
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
