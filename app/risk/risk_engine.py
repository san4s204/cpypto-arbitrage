from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.domain.models import Quote, Signal


@dataclass(frozen=True, slots=True)
class RiskConfig:
    max_open_positions: int = 4
    max_notional_per_position: float = 250.0
    max_total_exposure: float = 750.0
    max_daily_loss: float = 60.0
    max_quote_age_seconds: float = 3.0
    min_signal_confidence: float = 0.25
    cooldown_seconds: float = 15.0

    @classmethod
    def from_mapping(cls, payload: dict) -> RiskConfig:
        risk = payload.get("risk", payload)
        return cls(
            max_open_positions=int(risk.get("max_open_positions", 4)),
            max_notional_per_position=float(
                risk.get("max_notional_per_position", 250)
            ),
            max_total_exposure=float(risk.get("max_total_exposure", 750)),
            max_daily_loss=float(risk.get("max_daily_loss", 60)),
            max_quote_age_seconds=float(risk.get("max_quote_age_seconds", 3)),
            min_signal_confidence=float(risk.get("min_signal_confidence", 0.25)),
            cooldown_seconds=float(risk.get("cooldown_seconds", 15)),
        )


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reason: str = "ok"


class RiskEngine:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config
        self._last_entry_at: dict[tuple[str, str, str], datetime] = {}

    def assess(
        self,
        signal: Signal,
        quote: Quote,
        *,
        notional: float,
        open_positions: int,
        total_exposure: float,
        daily_realized_pnl: float,
    ) -> RiskDecision:
        if not signal.action.is_entry:
            return RiskDecision(True)
        if quote.exchange != signal.exchange or quote.symbol != signal.symbol:
            return RiskDecision(False, "signal and quote do not match")
        if notional <= 0:
            return RiskDecision(False, "no equity available for a new position")
        if quote.age_seconds() > self.config.max_quote_age_seconds:
            return RiskDecision(False, "stale quote")
        if signal.confidence < self.config.min_signal_confidence:
            return RiskDecision(False, "signal confidence below threshold")
        if open_positions >= self.config.max_open_positions:
            return RiskDecision(False, "maximum number of open positions reached")
        if notional > self.config.max_notional_per_position:
            return RiskDecision(False, "position notional exceeds limit")
        if total_exposure + notional > self.config.max_total_exposure:
            return RiskDecision(False, "total exposure limit reached")
        if daily_realized_pnl <= -self.config.max_daily_loss:
            return RiskDecision(False, "daily loss limit reached")

        key = signal.strategy_id, signal.exchange, signal.symbol
        last_entry = self._last_entry_at.get(key)
        if last_entry is not None:
            elapsed = (signal.created_at - last_entry).total_seconds()
            if elapsed < self.config.cooldown_seconds:
                return RiskDecision(False, "strategy cooldown is active")
        return RiskDecision(True)

    def record_entry(self, signal: Signal) -> None:
        self._last_entry_at[
            signal.strategy_id, signal.exchange, signal.symbol
        ] = signal.created_at
