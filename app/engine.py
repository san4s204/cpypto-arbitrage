from __future__ import annotations

import logging
from collections import defaultdict

from app.analytics.metrics import PerformanceMetrics, calculate_metrics
from app.analytics.storage import TradeStore
from app.bot.telegram import Notifier
from app.domain.models import ClosedTrade, PositionSide, Quote, Signal, SignalAction
from app.risk.position_sizer import PositionSizer
from app.risk.risk_engine import RiskEngine
from app.strategies.base import BaseStrategy
from app.trading.paper_trader import PaperTrader

logger = logging.getLogger(__name__)


class TradingEngine:
    def __init__(
        self,
        *,
        strategies: list[BaseStrategy],
        risk_engine: RiskEngine,
        position_sizer: PositionSizer,
        paper_trader: PaperTrader,
        trade_store: TradeStore,
        notifier: Notifier,
    ) -> None:
        self.strategies = strategies
        self.risk_engine = risk_engine
        self.position_sizer = position_sizer
        self.paper_trader = paper_trader
        self.trade_store = trade_store
        self.notifier = notifier
        self.quote_cache: dict[tuple[str, str], Quote] = {}

    @property
    def metrics(self) -> PerformanceMetrics:
        return calculate_metrics(
            self.paper_trader.closed_trades,
            initial_equity=self.paper_trader.initial_equity,
        )

    async def handle_quote(self, quote: Quote) -> None:
        self.quote_cache[(quote.exchange, quote.symbol)] = quote
        for trade in self.paper_trader.mark_quote(quote):
            await self._record_closed_trade(trade, close_group_peers=True)

        for strategy in self.strategies:
            signals = strategy.on_quote(quote, self.paper_trader.context)
            if signals:
                await self._process_signal_batch(signals)

    async def _process_signal_batch(self, signals: list[Signal]) -> None:
        grouped: dict[str, list[Signal]] = defaultdict(list)
        singles: list[Signal] = []
        for signal in signals:
            group_id = signal.metadata.get("group_id")
            if group_id and signal.action.is_entry:
                grouped[str(group_id)].append(signal)
            else:
                singles.append(signal)

        for signal in singles:
            await self._process_single_signal(signal)
        for group in grouped.values():
            await self._process_atomic_entry_group(group)

    async def _process_single_signal(self, signal: Signal) -> None:
        quote = self.quote_cache.get((signal.exchange, signal.symbol))
        if quote is None:
            await self.notifier.signal_rejected(signal, "matching quote is unavailable")
            return

        if not signal.action.is_entry:
            trade = self.paper_trader.handle_signal(signal, quote)
            if trade is not None:
                await self._record_closed_trade(trade, close_group_peers=True)
            return

        key = signal.strategy_id, signal.exchange, signal.symbol
        if key in self.paper_trader.positions:
            await self.notifier.signal_rejected(signal, "position already exists")
            return

        notional = self.position_sizer.size(self.paper_trader.equity)
        decision = self.risk_engine.assess(
            signal,
            quote,
            notional=notional,
            open_positions=len(self.paper_trader.positions),
            total_exposure=self.paper_trader.total_exposure,
            daily_realized_pnl=self.paper_trader.daily_realized_pnl(),
        )
        if not decision.allowed:
            await self.notifier.signal_rejected(signal, decision.reason)
            return

        self.paper_trader.handle_signal(signal, quote, notional=notional)
        self.risk_engine.record_entry(signal)
        await self.notifier.position_opened(
            signal,
            notional=notional,
            price=quote.ask
            if signal.action is SignalAction.ENTER_LONG
            else quote.bid,
        )
        logger.info(
            "paper entry: %s %s %s %s %.2f USDT",
            signal.strategy_id,
            signal.action,
            signal.exchange,
            signal.symbol,
            notional,
        )

    async def _process_atomic_entry_group(self, signals: list[Signal]) -> None:
        notional = self.position_sizer.size(self.paper_trader.equity)
        projected_positions = len(self.paper_trader.positions)
        projected_exposure = self.paper_trader.total_exposure
        prepared: list[tuple[Signal, Quote]] = []

        for signal in signals:
            quote = self.quote_cache.get((signal.exchange, signal.symbol))
            if quote is None:
                await self.notifier.signal_rejected(
                    signal, "atomic group is missing a matching quote"
                )
                return
            key = signal.strategy_id, signal.exchange, signal.symbol
            if key in self.paper_trader.positions:
                await self.notifier.signal_rejected(
                    signal, "atomic group position already exists"
                )
                return
            decision = self.risk_engine.assess(
                signal,
                quote,
                notional=notional,
                open_positions=projected_positions,
                total_exposure=projected_exposure,
                daily_realized_pnl=self.paper_trader.daily_realized_pnl(),
            )
            if not decision.allowed:
                await self.notifier.signal_rejected(signal, decision.reason)
                return
            prepared.append((signal, quote))
            projected_positions += 1
            projected_exposure += notional

        for signal, quote in prepared:
            self.paper_trader.handle_signal(signal, quote, notional=notional)
            self.risk_engine.record_entry(signal)
            await self.notifier.position_opened(
                signal,
                notional=notional,
                price=quote.ask
                if signal.action is SignalAction.ENTER_LONG
                else quote.bid,
            )
        logger.info("opened atomic paper group with %d legs", len(prepared))

    async def _record_closed_trade(
        self,
        trade: ClosedTrade,
        *,
        close_group_peers: bool,
    ) -> None:
        self.trade_store.save_trade(trade)
        await self.notifier.trade_closed(trade)
        logger.info(
            "paper exit: %s %s %s pnl=%+.2f",
            trade.strategy_id,
            trade.exchange,
            trade.symbol,
            trade.net_pnl,
        )
        if not close_group_peers or not trade.group_id:
            return

        peers = [
            position
            for position in self.paper_trader.positions.values()
            if position.group_id == trade.group_id
        ]
        for position in peers:
            quote = self.quote_cache.get((position.exchange, position.symbol))
            if quote is None:
                continue
            action = (
                SignalAction.EXIT_LONG
                if position.side is PositionSide.LONG
                else SignalAction.EXIT_SHORT
            )
            peer_trade = self.paper_trader.handle_signal(
                Signal(
                    strategy_id=position.strategy_id,
                    action=action,
                    exchange=position.exchange,
                    symbol=position.symbol,
                    reason="paired leg closed",
                    created_at=quote.received_at,
                ),
                quote,
            )
            if peer_trade is not None:
                await self._record_closed_trade(peer_trade, close_group_peers=False)
