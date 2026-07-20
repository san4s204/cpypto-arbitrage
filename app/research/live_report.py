from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from math import sqrt
from pathlib import Path
from statistics import mean, median

from app.research.live_store import StoredFrame, StoredQuote
from app.research.universe_selector import percentile


@dataclass(frozen=True, slots=True)
class SpreadTradeResult:
    net_pnl_bps: float
    hold_seconds: float


@dataclass(frozen=True, slots=True)
class LivePairMetrics:
    symbol: str
    samples: int
    coverage: float
    mean_gross_spread_bps: float
    p95_gross_spread_bps: float
    max_gross_spread_bps: float
    spread_trades: int
    spread_win_rate: float
    spread_average_pnl_bps: float
    spread_total_pnl_bps: float
    spread_average_hold_seconds: float
    spread_score: float
    spread_candidate: bool
    p95_move_60s_bps: float
    p95_move_300s_bps: float
    micro_round_trip_cost_bps: float
    micro_edge_bps: float
    micro_score: float
    micro_candidate: bool
    latency_events: int
    latency_follow_rate: float
    latency_median_delay_seconds: float
    latency_score: float
    latency_candidate: bool

    def as_row(self) -> dict[str, str | int | float | bool]:
        return asdict(self)


def _mid(quote: StoredQuote) -> float:
    return (quote.bid + quote.ask) / 2


def _best_route(frame: StoredFrame) -> tuple[str, str, float] | None:
    if len(frame.quotes) < 2:
        return None
    buy_exchange, buy_quote = min(
        frame.quotes.items(),
        key=lambda item: item[1].ask,
    )
    sell_exchange, sell_quote = max(
        frame.quotes.items(),
        key=lambda item: item[1].bid,
    )
    if buy_exchange == sell_exchange:
        return None
    spread_bps = (sell_quote.bid / buy_quote.ask - 1) * 10_000
    return buy_exchange, sell_exchange, spread_bps


def _fixed_route_spread_bps(
    frame: StoredFrame,
    buy_exchange: str,
    sell_exchange: str,
) -> float | None:
    buy_quote = frame.quotes.get(buy_exchange)
    sell_quote = frame.quotes.get(sell_exchange)
    if buy_quote is None or sell_quote is None:
        return None
    return (sell_quote.bid / buy_quote.ask - 1) * 10_000


def _paired_trade_pnl_bps(
    entry: StoredFrame,
    exit_frame: StoredFrame,
    *,
    buy_exchange: str,
    sell_exchange: str,
    fee_bps: Mapping[str, float],
    slippage_bps: float,
) -> float:
    slippage = slippage_bps / 10_000
    buy_entry_quote = entry.quotes[buy_exchange]
    sell_entry_quote = entry.quotes[sell_exchange]
    buy_exit_quote = exit_frame.quotes[buy_exchange]
    sell_exit_quote = exit_frame.quotes[sell_exchange]

    long_entry = buy_entry_quote.ask * (1 + slippage)
    short_entry = sell_entry_quote.bid * (1 - slippage)
    long_exit = buy_exit_quote.bid * (1 - slippage)
    short_exit = sell_exit_quote.ask * (1 + slippage)
    long_quantity = 1 / long_entry
    short_quantity = 1 / short_entry

    gross_pnl = (
        (long_exit - long_entry) * long_quantity
        + (short_entry - short_exit) * short_quantity
    )
    fees = (
        float(fee_bps[buy_exchange]) / 10_000
        * (1 + long_exit * long_quantity)
        + float(fee_bps[sell_exchange]) / 10_000
        * (1 + short_exit * short_quantity)
    )
    return (gross_pnl - fees) * 10_000


def simulate_spread_trades(
    frames: Sequence[StoredFrame],
    *,
    entry_bps: float,
    exit_bps: float,
    max_hold_seconds: float,
    max_gap_seconds: float,
    fee_bps: Mapping[str, float],
    slippage_bps: float,
) -> list[SpreadTradeResult]:
    trades: list[SpreadTradeResult] = []
    index = 0
    while index < len(frames) - 1:
        route = _best_route(frames[index])
        if route is None or route[2] < entry_bps:
            index += 1
            continue
        buy_exchange, sell_exchange, _ = route
        exit_index: int | None = None
        scan = index + 1
        while scan < len(frames):
            gap_seconds = (
                frames[scan].sampled_at_ms - frames[scan - 1].sampled_at_ms
            ) / 1_000
            if gap_seconds > max_gap_seconds:
                break
            hold_seconds = (
                frames[scan].sampled_at_ms - frames[index].sampled_at_ms
            ) / 1_000
            route_spread = _fixed_route_spread_bps(
                frames[scan],
                buy_exchange,
                sell_exchange,
            )
            if route_spread is None:
                break
            if route_spread <= exit_bps or hold_seconds >= max_hold_seconds:
                exit_index = scan
                break
            scan += 1
        if exit_index is None:
            index = max(scan, index + 1)
            continue
        hold_seconds = (
            frames[exit_index].sampled_at_ms - frames[index].sampled_at_ms
        ) / 1_000
        trades.append(
            SpreadTradeResult(
                net_pnl_bps=_paired_trade_pnl_bps(
                    frames[index],
                    frames[exit_index],
                    buy_exchange=buy_exchange,
                    sell_exchange=sell_exchange,
                    fee_bps=fee_bps,
                    slippage_bps=slippage_bps,
                ),
                hold_seconds=hold_seconds,
            )
        )
        index = exit_index + 1
    return trades


def _composite_mid(frame: StoredFrame) -> float:
    return mean(_mid(quote) for quote in frame.quotes.values())


def _horizon_moves(
    frames: Sequence[StoredFrame],
    *,
    horizon_seconds: float,
    sample_interval_seconds: float,
) -> list[float]:
    moves: list[float] = []
    target_index = 1
    tolerance_seconds = max(2 * sample_interval_seconds, 1)
    for index, frame in enumerate(frames):
        target_ms = frame.sampled_at_ms + int(horizon_seconds * 1_000)
        target_index = max(target_index, index + 1)
        while (
            target_index < len(frames)
            and frames[target_index].sampled_at_ms < target_ms
        ):
            target_index += 1
        if target_index >= len(frames):
            break
        actual_horizon = (
            frames[target_index].sampled_at_ms - frame.sampled_at_ms
        ) / 1_000
        if actual_horizon > horizon_seconds + tolerance_seconds:
            continue
        start = _composite_mid(frame)
        end = _composite_mid(frames[target_index])
        moves.append(abs(end / start - 1) * 10_000)
    return moves


def _latency_statistics(
    frames: Sequence[StoredFrame],
    *,
    leader_move_bps: float,
    min_gap_bps: float,
    response_bps: float,
    response_window_seconds: float,
    max_gap_seconds: float,
) -> tuple[int, float, float]:
    events = 0
    response_delays: list[float] = []
    index = 1
    while index < len(frames):
        previous = frames[index - 1]
        current = frames[index]
        gap_seconds = (current.sampled_at_ms - previous.sampled_at_ms) / 1_000
        common_exchanges = set(previous.quotes) & set(current.quotes)
        if gap_seconds > max_gap_seconds or len(common_exchanges) < 2:
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
        if (
            abs(leader_move) < leader_move_bps
            or abs(leader_move - follower_move) < min_gap_bps
        ):
            index += 1
            continue

        events += 1
        direction = 1 if leader_move > 0 else -1
        follower_start = _mid(current.quotes[follower_exchange])
        scan = index + 1
        responded_at: int | None = None
        while scan < len(frames):
            elapsed = (frames[scan].sampled_at_ms - current.sampled_at_ms) / 1_000
            if elapsed > response_window_seconds:
                break
            quote = frames[scan].quotes.get(follower_exchange)
            if quote is not None:
                follower_response = (
                    direction * (_mid(quote) / follower_start - 1) * 10_000
                )
                if follower_response >= response_bps:
                    response_delays.append(elapsed)
                    responded_at = scan
                    break
            scan += 1
        index = (responded_at or scan) + 1

    follow_rate = len(response_delays) / events if events else 0.0
    median_delay = median(response_delays) if response_delays else 0.0
    return events, follow_rate, median_delay


def analyze_live_pair(
    symbol: str,
    frames: Sequence[StoredFrame],
    *,
    attempted_samples: int,
    sample_interval_seconds: float,
    fee_bps: Mapping[str, float],
    slippage_bps: float,
    spread_entry_bps: float = 65,
    spread_exit_bps: float = 8,
    spread_max_hold_seconds: float = 300,
    min_spread_trades: int = 3,
    latency_leader_move_bps: float = 12,
    latency_min_gap_bps: float = 8,
    latency_response_bps: float = 4,
    latency_response_window_seconds: float = 30,
) -> LivePairMetrics:
    routes = [route for frame in frames if (route := _best_route(frame)) is not None]
    gross_spreads = [route[2] for route in routes]
    max_gap_seconds = max(sample_interval_seconds * 3, 2)
    trades = simulate_spread_trades(
        frames,
        entry_bps=spread_entry_bps,
        exit_bps=spread_exit_bps,
        max_hold_seconds=spread_max_hold_seconds,
        max_gap_seconds=max_gap_seconds,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    trade_pnls = [trade.net_pnl_bps for trade in trades]
    spread_win_rate = (
        sum(pnl > 0 for pnl in trade_pnls) / len(trade_pnls) if trade_pnls else 0.0
    )
    spread_average_pnl = mean(trade_pnls) if trade_pnls else 0.0
    spread_score = (
        max(0.0, spread_average_pnl) * spread_win_rate * sqrt(len(trades))
        if trades
        else 0.0
    )
    spread_candidate = (
        len(trades) >= min_spread_trades
        and spread_average_pnl > 0
        and spread_win_rate >= 0.5
    )

    moves_60 = _horizon_moves(
        frames,
        horizon_seconds=60,
        sample_interval_seconds=sample_interval_seconds,
    )
    moves_300 = _horizon_moves(
        frames,
        horizon_seconds=300,
        sample_interval_seconds=sample_interval_seconds,
    )
    exchanges = tuple(frames[0].quotes) if frames else ()
    micro_cost = (
        mean(2 * float(fee_bps[exchange]) + 2 * slippage_bps for exchange in exchanges)
        if exchanges
        else 0.0
    )
    p95_move_60 = percentile(moves_60, 0.95)
    p95_move_300 = percentile(moves_300, 0.95)
    micro_edge = p95_move_300 - micro_cost
    micro_score = max(0.0, micro_edge) * p95_move_60
    micro_candidate = len(moves_300) >= 20 and micro_edge > 5

    latency_events, latency_follow_rate, latency_delay = _latency_statistics(
        frames,
        leader_move_bps=latency_leader_move_bps,
        min_gap_bps=latency_min_gap_bps,
        response_bps=latency_response_bps,
        response_window_seconds=latency_response_window_seconds,
        max_gap_seconds=max_gap_seconds,
    )
    latency_score = latency_follow_rate * sqrt(latency_events)
    latency_candidate = latency_events >= 3 and latency_follow_rate >= 0.5

    return LivePairMetrics(
        symbol=symbol,
        samples=len(frames),
        coverage=min(1.0, len(frames) / attempted_samples) if attempted_samples else 0.0,
        mean_gross_spread_bps=mean(gross_spreads) if gross_spreads else 0.0,
        p95_gross_spread_bps=percentile(gross_spreads, 0.95),
        max_gross_spread_bps=max(gross_spreads, default=0.0),
        spread_trades=len(trades),
        spread_win_rate=spread_win_rate,
        spread_average_pnl_bps=spread_average_pnl,
        spread_total_pnl_bps=sum(trade_pnls),
        spread_average_hold_seconds=(
            mean(trade.hold_seconds for trade in trades) if trades else 0.0
        ),
        spread_score=spread_score,
        spread_candidate=spread_candidate,
        p95_move_60s_bps=p95_move_60,
        p95_move_300s_bps=p95_move_300,
        micro_round_trip_cost_bps=micro_cost,
        micro_edge_bps=micro_edge,
        micro_score=micro_score,
        micro_candidate=micro_candidate,
        latency_events=latency_events,
        latency_follow_rate=latency_follow_rate,
        latency_median_delay_seconds=latency_delay,
        latency_score=latency_score,
        latency_candidate=latency_candidate,
    )


def write_live_report(path: Path, metrics: Sequence[LivePairMetrics]) -> None:
    if not metrics:
        raise ValueError("cannot write an empty live universe report")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(metrics[0].as_row()))
        writer.writeheader()
        writer.writerows(item.as_row() for item in metrics)


def print_live_rankings(metrics: Sequence[LivePairMetrics], top: int = 10) -> None:
    spread = sorted(metrics, key=lambda item: item.spread_score, reverse=True)
    micro = sorted(metrics, key=lambda item: item.micro_score, reverse=True)
    latency = sorted(metrics, key=lambda item: item.latency_score, reverse=True)

    print("\nLIVE SPREAD RANKING")
    print(
        f"{'#':>2} {'symbol':<14} {'coverage':>8} {'p95':>7} {'max':>7} "
        f"{'trades':>6} {'avg pnl':>8} {'wins':>7} {'ok':>3}"
    )
    for index, item in enumerate(spread[:top], start=1):
        print(
            f"{index:>2} {item.symbol:<14} {item.coverage:>7.1%} "
            f"{item.p95_gross_spread_bps:>6.1f}b "
            f"{item.max_gross_spread_bps:>6.1f}b "
            f"{item.spread_trades:>6} {item.spread_average_pnl_bps:>7.1f}b "
            f"{item.spread_win_rate:>6.1%} "
            f"{'yes' if item.spread_candidate else 'no':>3}"
        )

    print("\nMICRO-TREND VOLATILITY RANKING")
    print(
        f"{'#':>2} {'symbol':<14} {'p95 1m':>8} {'p95 5m':>8} "
        f"{'cost':>7} {'edge':>7} {'ok':>3}"
    )
    for index, item in enumerate(micro[:top], start=1):
        print(
            f"{index:>2} {item.symbol:<14} {item.p95_move_60s_bps:>7.1f}b "
            f"{item.p95_move_300s_bps:>7.1f}b "
            f"{item.micro_round_trip_cost_bps:>6.1f}b "
            f"{item.micro_edge_bps:>6.1f}b "
            f"{'yes' if item.micro_candidate else 'no':>3}"
        )

    print("\nLATENCY RANKING")
    print(
        f"{'#':>2} {'symbol':<14} {'events':>7} {'follow':>8} "
        f"{'delay':>8} {'ok':>3}"
    )
    for index, item in enumerate(latency[:top], start=1):
        print(
            f"{index:>2} {item.symbol:<14} {item.latency_events:>7} "
            f"{item.latency_follow_rate:>7.1%} "
            f"{item.latency_median_delay_seconds:>6.1f}s "
            f"{'yes' if item.latency_candidate else 'no':>3}"
        )
