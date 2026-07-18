from __future__ import annotations

from itertools import permutations
from uuid import uuid4

from app.domain.models import PositionSide, Quote, Signal, SignalAction, StrategyContext
from app.strategies.base import BaseStrategy, StrategyConfig


class SpreadReactionStrategy(BaseStrategy):
    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        params = config.parameters
        self.entry_bps = float(params.get("entry_bps", 25))
        self.exit_bps = float(params.get("exit_bps", 8))
        self.max_quote_age_seconds = float(params.get("max_quote_age_seconds", 2))
        self.max_hold_seconds = int(params.get("max_hold_seconds", 300))
        self._quotes: dict[str, dict[str, Quote]] = {}

    def _best_route(self, symbol: str) -> tuple[Quote, Quote, float] | None:
        quotes = list(self._quotes.get(symbol, {}).values())
        routes = [
            (buy, sell, (sell.bid / buy.ask - 1) * 10_000)
            for buy, sell in permutations(quotes, 2)
            if buy.exchange != sell.exchange
            and buy.age_seconds() <= self.max_quote_age_seconds
            and sell.age_seconds() <= self.max_quote_age_seconds
        ]
        return max(routes, key=lambda route: route[2], default=None)

    def _exit_signals(self, positions: tuple, reason: str) -> list[Signal]:
        return [
            Signal(
                strategy_id=self.strategy_id,
                action=(
                    SignalAction.EXIT_LONG
                    if position.side is PositionSide.LONG
                    else SignalAction.EXIT_SHORT
                ),
                exchange=position.exchange,
                symbol=position.symbol,
                reason=reason,
            )
            for position in positions
        ]

    def on_quote(self, quote: Quote, context: StrategyContext) -> list[Signal]:
        if not self.supports(quote):
            return []
        self._quotes.setdefault(quote.symbol, {})[quote.exchange] = quote
        route = self._best_route(quote.symbol)
        if route is None:
            return []

        buy_quote, sell_quote, spread_bps = route
        positions = context.for_symbol(self.strategy_id, quote.symbol)
        if positions:
            oldest = min(position.opened_at for position in positions)
            held_seconds = (quote.received_at - oldest).total_seconds()
            if spread_bps <= self.exit_bps:
                return self._exit_signals(
                    positions, f"cross-exchange spread compressed to {spread_bps:.1f} bps"
                )
            if held_seconds >= self.max_hold_seconds:
                return self._exit_signals(positions, "pair maximum holding time reached")
            return []

        if spread_bps < self.entry_bps:
            return []

        group_id = uuid4().hex
        confidence = min(1.0, spread_bps / max(self.entry_bps * 3, 1))
        common = {
            "strategy_id": self.strategy_id,
            "symbol": quote.symbol,
            "confidence": confidence,
            "max_hold_seconds": self.max_hold_seconds,
            "metadata": {"group_id": group_id, "spread_bps": spread_bps},
        }
        return [
            Signal(
                action=SignalAction.ENTER_LONG,
                exchange=buy_quote.exchange,
                reason=f"buy side of {spread_bps:.1f} bps cross-exchange spread",
                **common,
            ),
            Signal(
                action=SignalAction.ENTER_SHORT,
                exchange=sell_quote.exchange,
                reason=f"sell side of {spread_bps:.1f} bps cross-exchange spread",
                **common,
            ),
        ]
