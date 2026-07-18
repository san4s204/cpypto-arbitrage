from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _csv(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _fee_map(raw: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for item in raw.split(","):
        if not item.strip():
            continue
        exchange, value = item.split(":", maxsplit=1)
        result[exchange.strip()] = float(value)
    return result


@dataclass(frozen=True, slots=True)
class AppSettings:
    project_root: Path
    bot_mode: str = "paper"
    market_data_mode: str = "ws"
    exchanges: tuple[str, ...] = ("bybit", "okx")
    symbols: tuple[str, ...] = ("BTC/USDT", "ETH/USDT")
    risk_profile: str = "conservative"
    initial_equity: float = 3_000.0
    paper_fee_bps: dict[str, float] = field(
        default_factory=lambda: {"bybit": 10.0, "okx": 10.0}
    )
    paper_slippage_bps: float = 2.0
    sqlite_path: Path = Path("runtime/paper_trading.sqlite3")
    strategy_config_dir: Path = Path("strategies")
    telegram_token: str | None = None
    telegram_chat_ids: tuple[str, ...] = ()
    stats_interval_seconds: int = 60

    def __post_init__(self) -> None:
        if self.bot_mode != "paper":
            raise ValueError("MVP-1 supports BOT_MODE=paper only")
        if self.market_data_mode not in {"ws", "rest"}:
            raise ValueError("MARKET_DATA_MODE must be ws or rest")
        if self.initial_equity <= 0:
            raise ValueError("PAPER_INITIAL_EQUITY must be positive")
        if self.stats_interval_seconds <= 0:
            raise ValueError("STATS_INTERVAL_SECONDS must be positive")

    @classmethod
    def from_env(cls, project_root: Path | None = None) -> AppSettings:
        root = (project_root or Path(__file__).resolve().parents[2]).resolve()
        load_dotenv(root / ".env", override=False)

        db_path = Path(os.getenv("PAPER_DB_PATH", "runtime/paper_trading.sqlite3"))
        strategy_dir = Path(os.getenv("STRATEGY_CONFIG_DIR", "strategies"))
        default_fees = "bybit:10,okx:10,bitget:10,mexc:10"

        return cls(
            project_root=root,
            bot_mode=os.getenv("BOT_MODE", "paper").strip().lower(),
            market_data_mode=os.getenv("MARKET_DATA_MODE", "ws").strip().lower(),
            exchanges=_csv("EXCHANGES", "bybit,okx"),
            symbols=_csv("SYMBOLS", "BTC/USDT,ETH/USDT"),
            risk_profile=os.getenv("RISK_PROFILE", "conservative").strip(),
            initial_equity=float(os.getenv("PAPER_INITIAL_EQUITY", "3000")),
            paper_fee_bps=_fee_map(os.getenv("PAPER_FEE_BPS", default_fees)),
            paper_slippage_bps=float(os.getenv("PAPER_SLIPPAGE_BPS", "2")),
            sqlite_path=db_path if db_path.is_absolute() else root / db_path,
            strategy_config_dir=(
                strategy_dir if strategy_dir.is_absolute() else root / strategy_dir
            ),
            telegram_token=os.getenv("TELEGRAM_TOKEN") or None,
            telegram_chat_ids=_csv("TELEGRAM_CHAT_IDS", ""),
            stats_interval_seconds=int(os.getenv("STATS_INTERVAL_SECONDS", "60")),
        )
