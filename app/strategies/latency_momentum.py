from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime

from app.domain.models import PositionSide, Quote, Signal, SignalAction, StrategyContext
from app.strategies.base import BaseStrategy, StrategyConfig


class LatencyMomentumStrategy(BaseStrategy):
    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        params = config.parameters
        self.lookback_seconds = float(params.get("lookback_seconds", 5))
        self.leader_move_bps = float(params.get("leader_move_bps", 12))
        self.min_gap_bps = float(params.get("min_gap_bps", 8))
        self.exit_reversal_bps = float(params.get("exit_reversal_bps", 5))
        self.stop_loss_bps = float(params.get("stop_loss_bps", 30))
        self.take_profit_bps = float(params.get("take_profit_bps", 50))
        self.max_hold_seconds = int(params.get("max_hold_seconds", 120))
        self._history: dict[tuple[str, str], deque[tuple[datetime, float]]] = defaultdict(
            lambda: deque(maxlen=500)
        )

    def _return_bps(self, key: tuple[str, str], now: datetime) -> float | None:
        history = self._history[key]
        cutoff = now.timestamp() - self.lookback_seconds
        while len(history) > 2 and history[1][0].timestamp() < cutoff:
            history.popleft()
        if len(history) < 2:
            return None
        first = history[0][1]
        last = history[-1][1]
        return (last / first - 1) * 10_000

    def on_quote(self, quote: Quote, context: StrategyContext) -> list[Signal]:
        if not self.supports(quote):
            return []
        self._history[(quote.exchange, quote.symbol)].append((quote.occurred_at, quote.mid))

        returns: dict[str, float] = {}
        for exchange in self.config.exchanges:
            value = self._return_bps((exchange, quote.symbol), quote.occurred_at)
            if value is not None:
                returns[exchange] = value
        if len(returns) < 2:
            return []

        existing = context.for_symbol(self.strategy_id, quote.symbol)
        signals: list[Signal] = []
        for position in existing:
            move = returns.get(position.exchange)
            if move is None:
                continue
            is_reversal = (
                position.side is PositionSide.LONG and move <= -self.exit_reversal_bps
            ) or (
                position.side is PositionSide.SHORT and move >= self.exit_reversal_bps
            )
            if is_reversal:
                signals.append(
                    Signal(
                        strategy_id=self.strategy_id,
                        action=(
                            SignalAction.EXIT_LONG
                            if position.side is PositionSide.LONG
                            else SignalAction.EXIT_SHORT
                        ),
                        exchange=position.exchange,
                        symbol=position.symbol,
                        reason=f"follower reversed by {move:.1f} bps",
                    )
                )
        if existing:
            return signals

        leader_exchange, leader_move = max(returns.items(), key=lambda item: abs(item[1]))
        follower_exchange, follower_move = min(
            (
                (exchange, move)
                for exchange, move in returns.items()
                if exchange != leader_exchange
            ),
            key=lambda item: abs(item[1]),
        )
        gap_bps = abs(leader_move - follower_move)
        if abs(leader_move) < self.leader_move_bps or gap_bps < self.min_gap_bps:
            return []

        action = (
            SignalAction.ENTER_LONG if leader_move > 0 else SignalAction.ENTER_SHORT
        )
        return [
            Signal(
                strategy_id=self.strategy_id,
                action=action,
                exchange=follower_exchange,
                symbol=quote.symbol,
                reason=(
                    f"{leader_exchange} moved {leader_move:.1f} bps while "
                    f"{follower_exchange} moved {follower_move:.1f} bps"
                ),
                confidence=min(1.0, gap_bps / max(self.min_gap_bps * 3, 1)),
                stop_loss_bps=self.stop_loss_bps,
                take_profit_bps=self.take_profit_bps,
                max_hold_seconds=self.max_hold_seconds,
                metadata={"leader_exchange": leader_exchange},
            )
        ]

