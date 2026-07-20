from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from app.domain.models import (
    ClosedTrade,
    Position,
    PositionSide,
    Quote,
    Signal,
    SignalAction,
    StrategyContext,
    utc_now,
)


class PaperTrader:
    def __init__(
        self,
        *,
        initial_equity: float,
        fee_bps: dict[str, float],
        slippage_bps: float,
    ) -> None:
        self.initial_equity = initial_equity
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps
        self.positions: dict[tuple[str, str, str], Position] = {}
        self.closed_trades: list[ClosedTrade] = []
        self.latest_quotes: dict[tuple[str, str], Quote] = {}

    @property
    def context(self) -> StrategyContext:
        return StrategyContext(tuple(position.view() for position in self.positions.values()))

    @property
    def total_exposure(self) -> float:
        return sum(position.entry_notional for position in self.positions.values())

    @property
    def realized_pnl(self) -> float:
        return sum(trade.net_pnl for trade in self.closed_trades)

    def daily_realized_pnl(self, now: datetime | None = None) -> float:
        current_date = (now or utc_now()).date()
        return sum(
            trade.net_pnl
            for trade in self.closed_trades
            if trade.closed_at.date() == current_date
        )

    @property
    def equity(self) -> float:
        unrealized = 0.0
        for position in self.positions.values():
            quote = self.latest_quotes.get((position.exchange, position.symbol))
            if quote is None:
                continue
            exit_price = self._exit_price(position.side, quote)
            gross = self._gross_pnl(position, exit_price)
            estimated_fee = position.quantity * exit_price * self._fee_rate(
                position.exchange
            )
            unrealized += gross - position.entry_fee - estimated_fee
        return self.initial_equity + self.realized_pnl + unrealized

    def _fee_rate(self, exchange: str) -> float:
        return self.fee_bps.get(exchange, 10.0) / 10_000

    def _entry_price(self, side: PositionSide, quote: Quote) -> float:
        slip = self.slippage_bps / 10_000
        if side is PositionSide.LONG:
            return quote.ask * (1 + slip)
        return quote.bid * (1 - slip)

    def _exit_price(self, side: PositionSide, quote: Quote) -> float:
        slip = self.slippage_bps / 10_000
        if side is PositionSide.LONG:
            return quote.bid * (1 - slip)
        return quote.ask * (1 + slip)

    @staticmethod
    def _gross_pnl(position: Position, exit_price: float) -> float:
        if position.side is PositionSide.LONG:
            return (exit_price - position.entry_price) * position.quantity
        return (position.entry_price - exit_price) * position.quantity

    def handle_signal(
        self,
        signal: Signal,
        quote: Quote,
        *,
        notional: float | None = None,
    ) -> ClosedTrade | None:
        self.latest_quotes[(quote.exchange, quote.symbol)] = quote
        if signal.action.is_entry:
            if notional is None or notional <= 0:
                raise ValueError("entry signal requires a positive notional")
            self._open(signal, quote, notional)
            return None
        return self._close(signal, quote, signal.reason)

    def _open(self, signal: Signal, quote: Quote, notional: float) -> None:
        key = signal.strategy_id, signal.exchange, signal.symbol
        if key in self.positions:
            raise ValueError(f"position already exists for {key}")
        side = (
            PositionSide.LONG
            if signal.action is SignalAction.ENTER_LONG
            else PositionSide.SHORT
        )
        entry_price = self._entry_price(side, quote)
        quantity = notional / entry_price
        self.positions[key] = Position(
            strategy_id=signal.strategy_id,
            exchange=signal.exchange,
            symbol=signal.symbol,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            entry_notional=notional,
            entry_fee=notional * self._fee_rate(signal.exchange),
            opened_at=signal.created_at,
            stop_loss_bps=signal.stop_loss_bps,
            take_profit_bps=signal.take_profit_bps,
            max_hold_seconds=signal.max_hold_seconds,
            group_id=str(signal.metadata.get("group_id"))
            if signal.metadata.get("group_id")
            else None,
            last_price=quote.mid,
        )

    def _close(self, signal: Signal, quote: Quote, reason: str) -> ClosedTrade | None:
        key = signal.strategy_id, signal.exchange, signal.symbol
        position = self.positions.get(key)
        if position is None:
            return None
        expected_action = (
            SignalAction.EXIT_LONG
            if position.side is PositionSide.LONG
            else SignalAction.EXIT_SHORT
        )
        if signal.action is not expected_action:
            raise ValueError("exit signal has the wrong position side")

        exit_price = self._exit_price(position.side, quote)
        gross_pnl = self._gross_pnl(position, exit_price)
        exit_fee = position.quantity * exit_price * self._fee_rate(position.exchange)
        trade = ClosedTrade(
            trade_id=uuid4().hex,
            strategy_id=position.strategy_id,
            exchange=position.exchange,
            symbol=position.symbol,
            side=position.side,
            quantity=position.quantity,
            entry_price=position.entry_price,
            exit_price=exit_price,
            entry_notional=position.entry_notional,
            gross_pnl=gross_pnl,
            fees=position.entry_fee + exit_fee,
            net_pnl=gross_pnl - position.entry_fee - exit_fee,
            opened_at=position.opened_at,
            closed_at=quote.received_at,
            close_reason=reason,
            group_id=position.group_id,
        )
        del self.positions[key]
        self.closed_trades.append(trade)
        return trade

    def mark_quote(self, quote: Quote) -> list[ClosedTrade]:
        self.latest_quotes[(quote.exchange, quote.symbol)] = quote
        closed: list[ClosedTrade] = []
        matching = [
            position
            for position in self.positions.values()
            if position.exchange == quote.exchange and position.symbol == quote.symbol
        ]
        for position in matching:
            position.last_price = quote.mid
            exit_price = self._exit_price(position.side, quote)
            gross_pnl = self._gross_pnl(position, exit_price)
            exit_fee = position.quantity * exit_price * self._fee_rate(position.exchange)
            net_bps = (
                (gross_pnl - position.entry_fee - exit_fee)
                / position.entry_notional
                * 10_000
            )
            held_seconds = (quote.received_at - position.opened_at).total_seconds()
            reason: str | None = None
            if position.stop_loss_bps is not None and net_bps <= -position.stop_loss_bps:
                reason = "paper stop loss"
            elif (
                position.take_profit_bps is not None
                and net_bps >= position.take_profit_bps
            ):
                reason = "paper take profit"
            elif (
                position.max_hold_seconds is not None
                and held_seconds >= position.max_hold_seconds
            ):
                reason = "paper maximum holding time"
            if reason is None:
                continue
            action = (
                SignalAction.EXIT_LONG
                if position.side is PositionSide.LONG
                else SignalAction.EXIT_SHORT
            )
            trade = self._close(
                Signal(
                    strategy_id=position.strategy_id,
                    action=action,
                    exchange=position.exchange,
                    symbol=position.symbol,
                    reason=reason,
                    created_at=quote.received_at,
                ),
                quote,
                reason,
            )
            if trade is not None:
                closed.append(trade)
        return closed
