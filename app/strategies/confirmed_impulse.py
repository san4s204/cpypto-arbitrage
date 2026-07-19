from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
from statistics import mean

from app.domain.models import PositionSide, Quote, Signal, SignalAction, StrategyContext
from app.strategies.base import BaseStrategy, StrategyConfig


class ConfirmedImpulseStrategy(BaseStrategy):
    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        params = config.parameters
        self.lookback_seconds = float(params.get("lookback_seconds", 60))
        self.round_trip_cost_bps = float(params.get("round_trip_cost_bps", 24))
        self.safety_margin_bps = float(params.get("safety_margin_bps", 6))
        configured_move = float(params.get("min_confirmed_move_bps", 30))
        self.min_confirmed_move_bps = max(
            configured_move,
            self.round_trip_cost_bps + self.safety_margin_bps,
        )
        self.max_exchange_divergence_bps = float(
            params.get("max_exchange_divergence_bps", 10)
        )
        self.min_book_imbalance = float(params.get("min_book_imbalance", 0.15))
        self.min_trade_flow_imbalance = float(
            params.get("min_trade_flow_imbalance", 0.10)
        )
        self.exit_momentum_bps = float(params.get("exit_momentum_bps", 5))
        self.exit_book_imbalance = float(params.get("exit_book_imbalance", -0.05))
        self.exit_trade_flow_imbalance = float(
            params.get("exit_trade_flow_imbalance", -0.05)
        )
        self.max_quote_age_seconds = float(params.get("max_quote_age_seconds", 3))
        self.stop_loss_bps = float(params.get("stop_loss_bps", 60))
        self.take_profit_bps = float(params.get("take_profit_bps", 120))
        self.max_hold_seconds = int(params.get("max_hold_seconds", 300))
        if len(config.exchanges) < 2:
            raise ValueError("confirmed_impulse requires at least two exchanges")
        if self.lookback_seconds <= 0:
            raise ValueError("lookback_seconds must be positive")
        if self.round_trip_cost_bps < 0 or self.safety_margin_bps < 0:
            raise ValueError("cost and safety margin cannot be negative")
        if self.max_exchange_divergence_bps < 0:
            raise ValueError("max_exchange_divergence_bps cannot be negative")
        if not -1 <= self.min_book_imbalance <= 1:
            raise ValueError("min_book_imbalance must be between -1 and 1")
        if not -1 <= self.min_trade_flow_imbalance <= 1:
            raise ValueError("min_trade_flow_imbalance must be between -1 and 1")
        self._history: dict[tuple[str, str], deque[tuple[datetime, float]]] = defaultdict(
            deque
        )
        self._latest: dict[tuple[str, str], Quote] = {}

    def _append_price(self, quote: Quote) -> None:
        key = quote.exchange, quote.symbol
        history = self._history[key]
        timestamp = quote.received_at
        if history and timestamp < history[-1][0]:
            history.clear()
        if history and timestamp == history[-1][0]:
            history[-1] = timestamp, quote.mid
        else:
            history.append((timestamp, quote.mid))
        cutoff = timestamp.timestamp() - self.lookback_seconds
        while len(history) > 2 and history[1][0].timestamp() <= cutoff:
            history.popleft()

    def _return_bps(
        self,
        exchange: str,
        symbol: str,
        now: datetime,
    ) -> float | None:
        history = self._history[(exchange, symbol)]
        if len(history) < 2:
            return None
        covered_seconds = (now - history[0][0]).total_seconds()
        if covered_seconds < self.lookback_seconds * 0.8:
            return None
        return (history[-1][1] / history[0][1] - 1) * 10_000

    def _market_snapshot(
        self,
        symbol: str,
        now: datetime,
    ) -> tuple[list[Quote], dict[str, float]] | None:
        quotes: list[Quote] = []
        returns: dict[str, float] = {}
        for exchange in self.config.exchanges:
            quote = self._latest.get((exchange, symbol))
            if quote is None:
                return None
            age_seconds = max(0.0, (now - quote.received_at).total_seconds())
            if age_seconds > self.max_quote_age_seconds:
                return None
            move = self._return_bps(exchange, symbol, now)
            if move is None:
                return None
            quotes.append(quote)
            returns[exchange] = move
        return quotes, returns

    @staticmethod
    def _imbalances(
        quotes: list[Quote],
    ) -> tuple[float, float, float, float] | None:
        book = [quote.book_imbalance for quote in quotes]
        flow = [quote.trade_flow_imbalance for quote in quotes]
        if any(value is None for value in (*book, *flow)):
            return None
        book_values = [value for value in book if value is not None]
        flow_values = [value for value in flow if value is not None]
        return (
            mean(book_values),
            mean(flow_values),
            min(book_values),
            min(flow_values),
        )

    def on_quote(self, quote: Quote, context: StrategyContext) -> list[Signal]:
        if not self.supports(quote):
            return []
        self._latest[(quote.exchange, quote.symbol)] = quote
        self._append_price(quote)
        snapshot = self._market_snapshot(quote.symbol, quote.received_at)
        if snapshot is None:
            return []
        quotes, returns = snapshot
        imbalances = self._imbalances(quotes)
        if imbalances is None:
            return []
        book_imbalance, flow_imbalance, weakest_book, weakest_flow = imbalances
        average_move = mean(returns.values())

        existing = context.for_symbol(self.strategy_id, quote.symbol)
        if existing:
            flow_reversed = (
                book_imbalance <= self.exit_book_imbalance
                and flow_imbalance <= self.exit_trade_flow_imbalance
            )
            if average_move > self.exit_momentum_bps and not flow_reversed:
                return []
            return [
                Signal(
                    strategy_id=self.strategy_id,
                    action=SignalAction.EXIT_LONG,
                    exchange=position.exchange,
                    symbol=position.symbol,
                    reason=(
                        f"confirmed impulse faded to {average_move:.1f} bps; "
                        f"book={book_imbalance:+.2f} flow={flow_imbalance:+.2f}"
                    ),
                    created_at=quote.received_at,
                )
                for position in existing
                if position.side is PositionSide.LONG
            ]

        if min(returns.values()) < self.min_confirmed_move_bps:
            return []
        if max(returns.values()) - min(returns.values()) > self.max_exchange_divergence_bps:
            return []
        if weakest_book < self.min_book_imbalance:
            return []
        if weakest_flow < self.min_trade_flow_imbalance:
            return []

        execution_quote = min(quotes, key=lambda item: item.ask)
        excess_move = average_move - self.min_confirmed_move_bps
        confidence = min(
            1.0,
            0.4
            + excess_move / max(self.min_confirmed_move_bps * 2, 1)
            + max(0.0, book_imbalance - self.min_book_imbalance) / 2
            + max(0.0, flow_imbalance - self.min_trade_flow_imbalance) / 2,
        )
        return [
            Signal(
                strategy_id=self.strategy_id,
                action=SignalAction.ENTER_LONG,
                exchange=execution_quote.exchange,
                symbol=quote.symbol,
                reason=(
                    f"confirmed move {average_move:.1f} bps; "
                    f"book={book_imbalance:+.2f} flow={flow_imbalance:+.2f}"
                ),
                confidence=confidence,
                stop_loss_bps=self.stop_loss_bps,
                take_profit_bps=self.take_profit_bps,
                max_hold_seconds=self.max_hold_seconds,
                created_at=quote.received_at,
                metadata={
                    "confirmed_move_bps": average_move,
                    "book_imbalance": book_imbalance,
                    "trade_flow_imbalance": flow_imbalance,
                    "round_trip_cost_bps": self.round_trip_cost_bps,
                },
            )
        ]
