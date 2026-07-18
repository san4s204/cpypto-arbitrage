from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.domain.models import Quote, Signal, StrategyContext


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    strategy_id: str
    enabled: bool = True
    symbols: tuple[str, ...] = ()
    exchanges: tuple[str, ...] = ()
    parameters: dict[str, Any] = field(default_factory=dict)


class BaseStrategy(ABC):
    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self.strategy_id = config.strategy_id

    def supports(self, quote: Quote) -> bool:
        symbol_ok = not self.config.symbols or quote.symbol in self.config.symbols
        exchange_ok = not self.config.exchanges or quote.exchange in self.config.exchanges
        return self.config.enabled and symbol_ok and exchange_ok

    @abstractmethod
    def on_quote(self, quote: Quote, context: StrategyContext) -> list[Signal]:
        """Return zero or more trading intents for the latest quote."""

