from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.strategies.base import BaseStrategy, StrategyConfig
from app.strategies.confirmed_impulse import ConfirmedImpulseStrategy
from app.strategies.latency_momentum import LatencyMomentumStrategy
from app.strategies.micro_trend import MicroTrendStrategy
from app.strategies.spread_reaction import SpreadReactionStrategy

STRATEGY_TYPES: dict[str, type[BaseStrategy]] = {
    "confirmed_impulse": ConfirmedImpulseStrategy,
    "latency_momentum": LatencyMomentumStrategy,
    "micro_trend": MicroTrendStrategy,
    "spread_reaction": SpreadReactionStrategy,
}


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        payload = yaml.safe_load(file) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return payload


def load_strategies(config_dir: Path) -> list[BaseStrategy]:
    strategies: list[BaseStrategy] = []
    for path in sorted(config_dir.glob("*.yaml")):
        payload = _read_yaml(path)
        strategy_type = payload.get("type")
        if strategy_type is None:
            continue
        if strategy_type not in STRATEGY_TYPES:
            raise ValueError(f"unknown strategy type {strategy_type!r} in {path}")

        config = StrategyConfig(
            strategy_id=str(payload.get("id", path.stem)),
            enabled=bool(payload.get("enabled", True)),
            symbols=tuple(payload.get("symbols", ())),
            exchanges=tuple(payload.get("exchanges", ())),
            parameters=dict(payload.get("parameters", {})),
        )
        if config.enabled:
            strategies.append(STRATEGY_TYPES[strategy_type](config))
    return strategies


def load_risk_profile(config_dir: Path, profile_name: str) -> dict[str, Any]:
    path = config_dir / f"{profile_name}.yaml"
    payload = _read_yaml(path)
    if payload.get("profile") != profile_name:
        raise ValueError(f"{path} is not the {profile_name!r} risk profile")
    return payload
