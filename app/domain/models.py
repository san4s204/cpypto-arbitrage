from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


class SignalAction(StrEnum):
    ENTER_LONG = "enter_long"
    EXIT_LONG = "exit_long"
    ENTER_SHORT = "enter_short"
    EXIT_SHORT = "exit_short"

    @property
    def is_entry(self) -> bool:
        return self in {self.ENTER_LONG, self.ENTER_SHORT}


class PositionSide(StrEnum):
    LONG = "long"
    SHORT = "short"


@dataclass(frozen=True, slots=True)
class Quote:
    exchange: str
    symbol: str
    bid: float
    ask: float
    occurred_at: datetime
    received_at: datetime = field(default_factory=utc_now)
    bid_size: float | None = None
    ask_size: float | None = None
    buy_volume: float | None = None
    sell_volume: float | None = None
    trade_flow_window_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.bid <= 0 or self.ask <= 0:
            raise ValueError("bid and ask must be positive")
        if self.ask < self.bid:
            raise ValueError("ask must be greater than or equal to bid")
        for name in ("bid_size", "ask_size", "buy_volume", "sell_volume"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")
        if (
            self.trade_flow_window_seconds is not None
            and self.trade_flow_window_seconds <= 0
        ):
            raise ValueError("trade_flow_window_seconds must be positive")
        if self.occurred_at.tzinfo is None or self.received_at.tzinfo is None:
            raise ValueError("quote timestamps must be timezone-aware")

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * 10_000

    @property
    def book_imbalance(self) -> float | None:
        if self.bid_size is None or self.ask_size is None:
            return None
        total = self.bid_size + self.ask_size
        return (self.bid_size - self.ask_size) / total if total > 0 else None

    @property
    def trade_flow_imbalance(self) -> float | None:
        if self.buy_volume is None or self.sell_volume is None:
            return None
        total = self.buy_volume + self.sell_volume
        return (self.buy_volume - self.sell_volume) / total if total > 0 else None

    def age_seconds(self, now: datetime | None = None) -> float:
        current = now or utc_now()
        return max(0.0, (current - self.occurred_at).total_seconds())


@dataclass(frozen=True, slots=True)
class Signal:
    strategy_id: str
    action: SignalAction
    exchange: str
    symbol: str
    reason: str
    confidence: float = 1.0
    stop_loss_bps: float | None = None
    take_profit_bps: float | None = None
    max_hold_seconds: int | None = None
    created_at: datetime = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(slots=True)
class Position:
    strategy_id: str
    exchange: str
    symbol: str
    side: PositionSide
    quantity: float
    entry_price: float
    entry_notional: float
    entry_fee: float
    opened_at: datetime
    stop_loss_bps: float | None = None
    take_profit_bps: float | None = None
    max_hold_seconds: int | None = None
    group_id: str | None = None
    last_price: float | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return self.strategy_id, self.exchange, self.symbol

    def view(self) -> PositionView:
        return PositionView(
            strategy_id=self.strategy_id,
            exchange=self.exchange,
            symbol=self.symbol,
            side=self.side,
            opened_at=self.opened_at,
            group_id=self.group_id,
        )


@dataclass(frozen=True, slots=True)
class PositionView:
    strategy_id: str
    exchange: str
    symbol: str
    side: PositionSide
    opened_at: datetime
    group_id: str | None = None


@dataclass(frozen=True, slots=True)
class StrategyContext:
    positions: tuple[PositionView, ...] = ()

    def for_symbol(self, strategy_id: str, symbol: str) -> tuple[PositionView, ...]:
        return tuple(
            position
            for position in self.positions
            if position.strategy_id == strategy_id and position.symbol == symbol
        )

    def find(
        self,
        strategy_id: str,
        exchange: str,
        symbol: str,
    ) -> PositionView | None:
        return next(
            (
                position
                for position in self.positions
                if position.strategy_id == strategy_id
                and position.exchange == exchange
                and position.symbol == symbol
            ),
            None,
        )


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    trade_id: str
    strategy_id: str
    exchange: str
    symbol: str
    side: PositionSide
    quantity: float
    entry_price: float
    exit_price: float
    entry_notional: float
    gross_pnl: float
    fees: float
    net_pnl: float
    opened_at: datetime
    closed_at: datetime
    close_reason: str
    group_id: str | None = None

    @property
    def hold_seconds(self) -> float:
        return max(0.0, (self.closed_at - self.opened_at).total_seconds())

    @property
    def return_fraction(self) -> float:
        if self.entry_notional == 0:
            return 0.0
        return self.net_pnl / self.entry_notional
