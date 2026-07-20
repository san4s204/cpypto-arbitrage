from __future__ import annotations

import logging
from typing import Protocol

import httpx

from app.analytics.metrics import PerformanceMetrics
from app.domain.models import ClosedTrade, Signal

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    async def started(self, mode: str, strategy_ids: list[str]) -> None: ...

    async def position_opened(
        self,
        signal: Signal,
        *,
        notional: float,
        price: float,
    ) -> None: ...

    async def trade_closed(self, trade: ClosedTrade) -> None: ...

    async def signal_rejected(self, signal: Signal, reason: str) -> None: ...

    async def statistics(
        self,
        metrics: PerformanceMetrics,
        *,
        equity: float,
        daily_pnl: float,
        open_positions: int,
        strategy_ids: list[str],
    ) -> None: ...


class NullNotifier:
    async def started(self, mode: str, strategy_ids: list[str]) -> None:
        return None

    async def position_opened(
        self,
        signal: Signal,
        *,
        notional: float,
        price: float,
    ) -> None:
        return None

    async def trade_closed(self, trade: ClosedTrade) -> None:
        return None

    async def signal_rejected(self, signal: Signal, reason: str) -> None:
        return None

    async def statistics(
        self,
        metrics: PerformanceMetrics,
        *,
        equity: float,
        daily_pnl: float,
        open_positions: int,
        strategy_ids: list[str],
    ) -> None:
        return None


class TelegramNotifier:
    def __init__(self, token: str, chat_ids: tuple[str, ...]) -> None:
        if not token or not chat_ids:
            raise ValueError("Telegram token and at least one chat ID are required")
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_ids = chat_ids

    async def _send(self, text: str) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            for chat_id in self.chat_ids:
                try:
                    response = await client.post(
                        self.url,
                        json={"chat_id": chat_id, "text": text},
                    )
                    response.raise_for_status()
                except httpx.HTTPError as error:
                    logger.warning("Telegram notification failed: %s", error)

    async def started(self, mode: str, strategy_ids: list[str]) -> None:
        await self._send(
            "Strategy Lab started\n"
            f"Mode: {mode}\n"
            f"Strategies: {', '.join(strategy_ids)}"
        )

    async def position_opened(
        self,
        signal: Signal,
        *,
        notional: float,
        price: float,
    ) -> None:
        await self._send(
            "New paper signal\n"
            f"{signal.strategy_id} · {signal.action.value}\n"
            f"{signal.exchange} · {signal.symbol}\n"
            f"Notional: {notional:.2f} USDT · Price: {price:.8g}\n"
            f"Reason: {signal.reason}"
        )

    async def trade_closed(self, trade: ClosedTrade) -> None:
        await self._send(
            "Paper trade closed\n"
            f"{trade.strategy_id} · {trade.exchange} · {trade.symbol}\n"
            f"{trade.side.value}: {trade.net_pnl:+.2f} USDT\n"
            f"Fees: {trade.fees:.2f} · Hold: {trade.hold_seconds:.0f}s\n"
            f"Reason: {trade.close_reason}"
        )

    async def signal_rejected(self, signal: Signal, reason: str) -> None:
        logger.info(
            "Signal rejected: %s %s %s: %s",
            signal.strategy_id,
            signal.exchange,
            signal.symbol,
            reason,
        )

    async def statistics(
        self,
        metrics: PerformanceMetrics,
        *,
        equity: float,
        daily_pnl: float,
        open_positions: int,
        strategy_ids: list[str],
    ) -> None:
        profit_factor = (
            f"{metrics.profit_factor:.2f}"
            if metrics.profit_factor is not None
            else "n/a"
        )
        await self._send(
            "Paper trading statistics\n"
            f"Equity: {equity:.2f} USDT\n"
            f"Daily PnL: {daily_pnl:+.2f} · Total PnL: {metrics.pnl:+.2f}\n"
            f"Trades: {metrics.trade_count} · Open: {open_positions}\n"
            f"Win rate: {metrics.win_rate:.1%} · PF: {profit_factor}\n"
            f"Max DD: {metrics.max_drawdown:.2f} ({metrics.max_drawdown_pct:.1%})\n"
            f"Sharpe: {metrics.sharpe:.2f} · Expectancy: {metrics.expectancy:+.2f}\n"
            f"Strategies: {', '.join(strategy_ids)}"
        )
