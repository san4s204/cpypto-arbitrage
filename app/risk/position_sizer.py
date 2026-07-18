from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PositionSizerConfig:
    allocation_fraction: float = 0.05
    min_notional: float = 10.0
    max_notional: float = 250.0

    def __post_init__(self) -> None:
        if not 0 < self.allocation_fraction <= 1:
            raise ValueError("allocation_fraction must be in (0, 1]")
        if self.min_notional <= 0 or self.max_notional < self.min_notional:
            raise ValueError("invalid position notional bounds")


class PositionSizer:
    def __init__(self, config: PositionSizerConfig) -> None:
        self.config = config

    def size(self, equity: float) -> float:
        if equity <= 0:
            return 0.0
        requested = equity * self.config.allocation_fraction
        return min(
            equity,
            self.config.max_notional,
            max(self.config.min_notional, requested),
        )
