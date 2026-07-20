from __future__ import annotations

import csv
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from math import sqrt
from pathlib import Path
from statistics import median

from app.research.live_store import StoredFrame, StoredQuote
from app.research.replay_latency import (
    LatencyReplayParameters,
    directed_routes,
    replay_latency_route,
)
from app.research.universe_selector import percentile


@dataclass(frozen=True, slots=True)
class LiveLatencyRouteMetrics:
    symbol: str
    leader_exchange: str
    follower_exchange: str
    samples: int
    events: int
    median_initial_gap_bps: float
    p95_initial_gap_bps: float
    follow_rate: float
    median_delay_seconds: float
    response_target_bps: float
    trade_count: int
    win_rate: float
    profit_factor: float | None
    expectancy_bps: float
    total_pnl_bps: float
    score: float
    candidate: bool

    def as_row(self) -> dict[str, str | int | float | bool | None]:
        return asdict(self)


def _mid(quote: StoredQuote) -> float:
    return (quote.bid + quote.ask) / 2


def _is_fresh(
    frame: StoredFrame,
    quote: StoredQuote,
    *,
    max_quote_age_seconds: float,
) -> bool:
    sampled_at = datetime.fromtimestamp(frame.sampled_at_ms / 1_000, tz=UTC)
    age = max(0.0, (sampled_at - quote.received_at).total_seconds())
    return age <= max_quote_age_seconds


def _behavior_statistics(
    frames: Sequence[StoredFrame],
    *,
    leader_exchange: str,
    follower_exchange: str,
    lookback_seconds: float,
    leader_move_bps: float,
    min_gap_bps: float,
    response_bps: float,
    response_window_seconds: float,
    max_quote_age_seconds: float,
    max_gap_seconds: float,
) -> tuple[int, list[float], list[float]]:
    timestamps = [frame.sampled_at_ms for frame in frames]
    lookback_ms = int(lookback_seconds * 1_000)
    max_gap_ms = int(max_gap_seconds * 1_000)
    initial_gaps: list[float] = []
    response_delays: list[float] = []
    index = 1

    while index < len(frames):
        current = frames[index]
        previous_index = bisect_right(
            timestamps,
            current.sampled_at_ms - lookback_ms,
            hi=index,
        ) - 1
        if previous_index < 0:
            index += 1
            continue
        previous = frames[previous_index]
        if current.sampled_at_ms - previous.sampled_at_ms > lookback_ms + max_gap_ms:
            index += 1
            continue
        route = (leader_exchange, follower_exchange)
        if not all(
            exchange in previous.quotes
            and exchange in current.quotes
            and _is_fresh(
                previous,
                previous.quotes[exchange],
                max_quote_age_seconds=max_quote_age_seconds,
            )
            and _is_fresh(
                current,
                current.quotes[exchange],
                max_quote_age_seconds=max_quote_age_seconds,
            )
            for exchange in route
        ):
            index += 1
            continue

        leader_move = (
            _mid(current.quotes[leader_exchange])
            / _mid(previous.quotes[leader_exchange])
            - 1
        ) * 10_000
        follower_move = (
            _mid(current.quotes[follower_exchange])
            / _mid(previous.quotes[follower_exchange])
            - 1
        ) * 10_000
        direction = 1 if leader_move > 0 else -1
        directional_gap = direction * (leader_move - follower_move)
        if abs(leader_move) < leader_move_bps or directional_gap < min_gap_bps:
            index += 1
            continue

        initial_gaps.append(directional_gap)
        follower_start = _mid(current.quotes[follower_exchange])
        scan = index + 1
        responded_at: int | None = None
        while scan < len(frames):
            candidate = frames[scan]
            if candidate.sampled_at_ms - frames[scan - 1].sampled_at_ms > max_gap_ms:
                break
            elapsed = (candidate.sampled_at_ms - current.sampled_at_ms) / 1_000
            if elapsed > response_window_seconds:
                break
            quote = candidate.quotes.get(follower_exchange)
            if quote is not None and _is_fresh(
                candidate,
                quote,
                max_quote_age_seconds=max_quote_age_seconds,
            ):
                follower_response = direction * (_mid(quote) / follower_start - 1) * 10_000
                if follower_response >= response_bps:
                    response_delays.append(elapsed)
                    responded_at = scan
                    break
            scan += 1
        index = (responded_at if responded_at is not None else scan) + 1

    return len(initial_gaps), initial_gaps, response_delays


def analyze_latency_route(
    *,
    session_id: str,
    symbol: str,
    leader_exchange: str,
    follower_exchange: str,
    frames: Sequence[StoredFrame],
    sample_interval_seconds: float,
    fee_bps: Mapping[str, float],
    slippage_bps: float,
    notional: float = 100,
    leader_move_bps: float = 12,
    min_gap_bps: float = 8,
    behavior_response_bps: float = 4,
    response_window_seconds: float = 5,
    safety_margin_bps: float = 5,
    stop_move_bps: float = 30,
    max_hold_seconds: float = 5,
    max_quote_age_seconds: float = 2,
    min_trades: int = 3,
    min_profit_factor: float = 1.2,
) -> LiveLatencyRouteMetrics:
    lookback_seconds = max(0.5, sample_interval_seconds)
    max_gap_seconds = max(2.0, sample_interval_seconds * 3)
    events, gaps, delays = _behavior_statistics(
        frames,
        leader_exchange=leader_exchange,
        follower_exchange=follower_exchange,
        lookback_seconds=lookback_seconds,
        leader_move_bps=leader_move_bps,
        min_gap_bps=min_gap_bps,
        response_bps=behavior_response_bps,
        response_window_seconds=response_window_seconds,
        max_quote_age_seconds=max_quote_age_seconds,
        max_gap_seconds=max_gap_seconds,
    )
    response_target_bps = (
        2 * float(fee_bps[follower_exchange])
        + 2 * slippage_bps
        + safety_margin_bps
    )
    run = replay_latency_route(
        session_id=session_id,
        symbol=symbol,
        leader_exchange=leader_exchange,
        follower_exchange=follower_exchange,
        frames=frames,
        parameters=LatencyReplayParameters(
            lookback_seconds=lookback_seconds,
            leader_move_bps=leader_move_bps,
            min_gap_bps=min_gap_bps,
            response_bps=response_target_bps,
            stop_move_bps=stop_move_bps,
            max_hold_seconds=max_hold_seconds,
            cooldown_seconds=1,
            max_quote_age_seconds=max_quote_age_seconds,
        ),
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        notional=notional,
        min_trades=min_trades,
        min_profit_factor=min_profit_factor,
        max_gap_seconds=max_gap_seconds,
    )
    metrics = run.metrics
    follow_rate = len(delays) / events if events else 0.0
    score = max(0.0, metrics.expectancy_bps) * sqrt(metrics.trade_count) * follow_rate
    return LiveLatencyRouteMetrics(
        symbol=symbol,
        leader_exchange=leader_exchange,
        follower_exchange=follower_exchange,
        samples=len(frames),
        events=events,
        median_initial_gap_bps=median(gaps) if gaps else 0.0,
        p95_initial_gap_bps=percentile(gaps, 0.95),
        follow_rate=follow_rate,
        median_delay_seconds=median(delays) if delays else 0.0,
        response_target_bps=response_target_bps,
        trade_count=metrics.trade_count,
        win_rate=metrics.win_rate,
        profit_factor=metrics.profit_factor,
        expectancy_bps=metrics.expectancy_bps,
        total_pnl_bps=metrics.total_pnl_bps,
        score=score,
        candidate=metrics.candidate,
    )


def analyze_latency_routes(
    *,
    session_id: str,
    symbol_frames: Mapping[str, Sequence[StoredFrame]],
    exchanges: Sequence[str],
    sample_interval_seconds: float,
    fee_bps: Mapping[str, float],
    slippage_bps: float,
) -> list[LiveLatencyRouteMetrics]:
    return [
        analyze_latency_route(
            session_id=session_id,
            symbol=symbol,
            leader_exchange=leader,
            follower_exchange=follower,
            frames=frames,
            sample_interval_seconds=sample_interval_seconds,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
        )
        for symbol, frames in symbol_frames.items()
        for leader, follower in directed_routes(exchanges)
        if any(leader in frame.quotes and follower in frame.quotes for frame in frames)
    ]


def write_latency_route_report(
    path: Path,
    metrics: Sequence[LiveLatencyRouteMetrics],
) -> None:
    if not metrics:
        raise ValueError("cannot write an empty latency route report")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(metrics[0].as_row()))
        writer.writeheader()
        writer.writerows(item.as_row() for item in metrics)


def _profit_factor_label(value: float | None, trade_count: int) -> str:
    if value is None:
        return "inf" if trade_count else "-"
    return f"{value:.2f}"


def print_latency_route_ranking(
    metrics: Sequence[LiveLatencyRouteMetrics],
    *,
    top: int = 20,
) -> None:
    ranked = sorted(
        metrics,
        key=lambda item: (
            item.candidate,
            item.score,
            item.trade_count,
            item.expectancy_bps,
        ),
        reverse=True,
    )
    print("\nDIRECTED LATENCY ROUTES")
    print(
        f"{'#':>2} {'symbol':<14} {'route':<20} {'events':>7} {'gap50':>7} "
        f"{'follow':>8} {'delay':>7} {'trades':>7} {'PF':>6} "
        f"{'expect':>9} {'ok':>3}"
    )
    for index, item in enumerate(ranked[:top], start=1):
        route = f"{item.leader_exchange}→{item.follower_exchange}"
        print(
            f"{index:>2} {item.symbol:<14} {route:<20} {item.events:>7} "
            f"{item.median_initial_gap_bps:>6.1f}b {item.follow_rate:>7.1%} "
            f"{item.median_delay_seconds:>5.2f}s {item.trade_count:>7} "
            f"{_profit_factor_label(item.profit_factor, item.trade_count):>6} "
            f"{item.expectancy_bps:>8.1f}b "
            f"{'yes' if item.candidate else 'no':>3}"
        )
