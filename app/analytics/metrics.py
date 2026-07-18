from __future__ import annotations

from dataclasses import asdict, dataclass
from math import sqrt
from statistics import mean, pstdev

from app.domain.models import ClosedTrade


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    pnl: float
    win_rate: float
    profit_factor: float | None
    max_drawdown: float
    max_drawdown_pct: float
    average_hold_seconds: float
    sharpe: float
    expectancy: float
    trade_count: int
    average_profit: float
    average_loss: float

    def as_dict(self) -> dict:
        return asdict(self)


def calculate_metrics(
    trades: list[ClosedTrade],
    *,
    initial_equity: float,
) -> PerformanceMetrics:
    if not trades:
        return PerformanceMetrics(0, 0, None, 0, 0, 0, 0, 0, 0, 0, 0)

    pnls = [trade.net_pnl for trade in trades]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss else None

    equity = initial_equity
    peak = initial_equity
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = peak - equity
        max_drawdown = max(max_drawdown, drawdown)
        if peak > 0:
            max_drawdown_pct = max(max_drawdown_pct, drawdown / peak)

    returns = [trade.return_fraction for trade in trades]
    volatility = pstdev(returns) if len(returns) > 1 else 0.0
    sharpe = mean(returns) / volatility * sqrt(len(returns)) if volatility else 0.0

    return PerformanceMetrics(
        pnl=sum(pnls),
        win_rate=len(wins) / len(trades),
        profit_factor=profit_factor,
        max_drawdown=max_drawdown,
        max_drawdown_pct=max_drawdown_pct,
        average_hold_seconds=mean(trade.hold_seconds for trade in trades),
        sharpe=sharpe,
        expectancy=mean(pnls),
        trade_count=len(trades),
        average_profit=mean(wins) if wins else 0.0,
        average_loss=mean(losses) if losses else 0.0,
    )

