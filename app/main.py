from __future__ import annotations

import asyncio
import logging
import time

from app.analytics.storage import TradeStore
from app.bot.telegram import NullNotifier, TelegramNotifier
from app.config.settings import AppSettings
from app.config.strategy_loader import load_risk_profile, load_strategies
from app.engine import TradingEngine
from app.market_data.rest_poller import RestMarketDataFeed
from app.market_data.ws_listener import CcxtProMarketDataFeed
from app.risk.position_sizer import PositionSizer, PositionSizerConfig
from app.risk.risk_engine import RiskConfig, RiskEngine
from app.trading.paper_trader import PaperTrader

logger = logging.getLogger(__name__)


def build_engine(settings: AppSettings) -> TradingEngine:
    profile = load_risk_profile(settings.strategy_config_dir, settings.risk_profile)
    strategies = load_strategies(settings.strategy_config_dir)
    if not strategies:
        raise RuntimeError("no enabled strategies were loaded")

    risk_config = RiskConfig.from_mapping(profile)
    sizing = profile.get("position_sizing", {})
    sizer_config = PositionSizerConfig(
        allocation_fraction=float(sizing.get("allocation_fraction", 0.05)),
        min_notional=float(sizing.get("min_notional", 10)),
        max_notional=min(
            float(sizing.get("max_notional", risk_config.max_notional_per_position)),
            risk_config.max_notional_per_position,
        ),
    )
    store = TradeStore(settings.sqlite_path)
    trader = PaperTrader(
        initial_equity=settings.initial_equity,
        fee_bps=settings.paper_fee_bps,
        slippage_bps=settings.paper_slippage_bps,
    )
    trader.closed_trades.extend(store.load_trades())
    notifier = (
        TelegramNotifier(settings.telegram_token, settings.telegram_chat_ids)
        if settings.telegram_token and settings.telegram_chat_ids
        else NullNotifier()
    )
    return TradingEngine(
        strategies=strategies,
        risk_engine=RiskEngine(risk_config),
        position_sizer=PositionSizer(sizer_config),
        paper_trader=trader,
        trade_store=store,
        notifier=notifier,
    )


async def run() -> None:
    settings = AppSettings.from_env()
    engine = build_engine(settings)
    feed = (
        CcxtProMarketDataFeed(
            exchanges=settings.exchanges,
            symbols=settings.symbols,
        )
        if settings.market_data_mode == "ws"
        else RestMarketDataFeed(
            exchanges=settings.exchanges,
            symbols=settings.symbols,
        )
    )
    logger.info(
        "starting strategy lab in %s mode with %d strategies",
        settings.bot_mode,
        len(engine.strategies),
    )
    strategy_ids = [strategy.strategy_id for strategy in engine.strategies]
    await engine.notifier.started(settings.bot_mode, strategy_ids)
    last_statistics = time.monotonic()
    async for quote in feed.quotes():
        await engine.handle_quote(quote)
        if time.monotonic() - last_statistics >= settings.stats_interval_seconds:
            await engine.notifier.statistics(
                engine.metrics,
                equity=engine.paper_trader.equity,
                daily_pnl=engine.paper_trader.daily_realized_pnl(),
                open_positions=len(engine.paper_trader.positions),
                strategy_ids=strategy_ids,
            )
            last_statistics = time.monotonic()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
