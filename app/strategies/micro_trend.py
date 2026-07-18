from __future__ import annotations

from collections import defaultdict, deque

from app.domain.models import PositionSide, Quote, Signal, SignalAction, StrategyContext
from app.strategies.base import BaseStrategy, StrategyConfig


class MicroTrendStrategy(BaseStrategy):
    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        params = config.parameters
        self.fast_window = int(params.get("fast_window", 5))
        self.slow_window = int(params.get("slow_window", 20))
        self.entry_bps = float(params.get("entry_bps", 5))
        self.exit_bps = float(params.get("exit_bps", 0))
        self.stop_loss_bps = float(params.get("stop_loss_bps", 35))
        self.take_profit_bps = float(params.get("take_profit_bps", 70))
        self.max_hold_seconds = int(params.get("max_hold_seconds", 900))
        if self.fast_window >= self.slow_window:
            raise ValueError("micro_trend fast_window must be less than slow_window")
        self._prices: dict[tuple[str, str], deque[float]] = defaultdict(
            lambda: deque(maxlen=self.slow_window)
        )

    def on_quote(self, quote: Quote, context: StrategyContext) -> list[Signal]:
        if not self.supports(quote):
            return []
        prices = self._prices[(quote.exchange, quote.symbol)]
        prices.append(quote.mid)
        if len(prices) < self.slow_window:
            return []

        fast = sum(list(prices)[-self.fast_window :]) / self.fast_window
        slow = sum(prices) / len(prices)
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
                )
            ]
        return []

