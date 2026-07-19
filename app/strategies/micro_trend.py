from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime

from app.domain.models import PositionSide, Quote, Signal, SignalAction, StrategyContext
from app.strategies.base import BaseStrategy, StrategyConfig


class MicroTrendStrategy(BaseStrategy):
    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        params = config.parameters
        fast_seconds = params.get("fast_seconds")
        slow_seconds = params.get("slow_seconds")
        if (fast_seconds is None) != (slow_seconds is None):
            raise ValueError(
                "micro_trend fast_seconds and slow_seconds must be configured together"
            )
        self.fast_seconds = float(fast_seconds) if fast_seconds is not None else None
        self.slow_seconds = float(slow_seconds) if slow_seconds is not None else None
        self.fast_window = int(params.get("fast_window", 5))
        self.slow_window = int(params.get("slow_window", 20))
        self.entry_bps = float(params.get("entry_bps", 5))
        self.exit_bps = float(params.get("exit_bps", 0))
        self.stop_loss_bps = float(params.get("stop_loss_bps", 35))
        self.take_profit_bps = float(params.get("take_profit_bps", 70))
        self.max_hold_seconds = int(params.get("max_hold_seconds", 900))
        if self.fast_seconds is not None and self.slow_seconds is not None:
            if self.fast_seconds <= 0 or self.fast_seconds >= self.slow_seconds:
                raise ValueError(
                    "micro_trend fast_seconds must be positive and less than slow_seconds"
                )
        elif self.fast_window <= 0 or self.fast_window >= self.slow_window:
            raise ValueError(
                "micro_trend fast_window must be positive and less than slow_window"
            )
        self._prices: dict[tuple[str, str], deque[float]] = defaultdict(
            lambda: deque(maxlen=self.slow_window)
        )
        self._timed_prices: dict[
            tuple[str, str], deque[tuple[datetime, float]]
        ] = defaultdict(deque)
        self._timed_started_at: dict[tuple[str, str], datetime] = {}

    def _moving_averages(self, quote: Quote) -> tuple[float, float] | None:
        key = quote.exchange, quote.symbol
        if self.fast_seconds is None or self.slow_seconds is None:
            prices = self._prices[key]
            prices.append(quote.mid)
            if len(prices) < self.slow_window:
                return None
            fast = sum(list(prices)[-self.fast_window :]) / self.fast_window
            return fast, sum(prices) / len(prices)

        history = self._timed_prices[key]
        timestamp = quote.received_at
        if history and (
            timestamp < history[-1][0]
            or (timestamp - history[-1][0]).total_seconds() > self.slow_seconds
        ):
            history.clear()
            self._timed_started_at[key] = timestamp

        if history and timestamp == history[-1][0]:
            history[-1] = timestamp, quote.mid
        else:
            history.append((timestamp, quote.mid))
        started_at = self._timed_started_at.setdefault(key, timestamp)

        slow_cutoff = timestamp.timestamp() - self.slow_seconds
        while history and history[0][0].timestamp() < slow_cutoff:
            history.popleft()
        if (timestamp - started_at).total_seconds() < self.slow_seconds:
            return None

        fast_cutoff = timestamp.timestamp() - self.fast_seconds
        fast_prices = [price for at, price in history if at.timestamp() >= fast_cutoff]
        if not fast_prices or not history:
            return None
        return (
            sum(fast_prices) / len(fast_prices),
            sum(price for _, price in history) / len(history),
        )

    def on_quote(self, quote: Quote, context: StrategyContext) -> list[Signal]:
        if not self.supports(quote):
            return []
        averages = self._moving_averages(quote)
        if averages is None:
            return []
        fast, slow = averages
        momentum_bps = (fast / slow - 1) * 10_000
        position = context.find(self.strategy_id, quote.exchange, quote.symbol)

        if position is None and momentum_bps >= self.entry_bps:
            return [
                Signal(
                    strategy_id=self.strategy_id,
                    action=SignalAction.ENTER_LONG,
                    exchange=quote.exchange,
                    symbol=quote.symbol,
                    reason=f"fast MA above slow MA by {momentum_bps:.1f} bps",
                    confidence=min(1.0, momentum_bps / max(self.entry_bps * 3, 1)),
                    stop_loss_bps=self.stop_loss_bps,
                    take_profit_bps=self.take_profit_bps,
                    max_hold_seconds=self.max_hold_seconds,
                    created_at=quote.received_at,
                )
            ]

        if (
            position is not None
            and position.side is PositionSide.LONG
            and momentum_bps <= self.exit_bps
        ):
            return [
                Signal(
                    strategy_id=self.strategy_id,
                    action=SignalAction.EXIT_LONG,
                    exchange=quote.exchange,
                    symbol=quote.symbol,
                    reason=f"micro trend reversed to {momentum_bps:.1f} bps",
                    created_at=quote.received_at,
                )
            ]
        return []
