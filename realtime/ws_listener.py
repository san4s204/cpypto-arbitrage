# realtime/ws_listener.py
import asyncio, json, os, signal, sys, time
from loguru import logger
import ccxt.pro as ccxtpro
import redis.asyncio as aioredis
from dotenv import load_dotenv
import pandas as pd, pathlib
load_dotenv()

# ─── конфиг ──────────────────────────────────────────────────────────────
raw_pairs = os.getenv("PAIRS", "")
PAIRS = [p.strip() for p in raw_pairs.split(",") if p.strip()]
POLL_SEC     = 3
REDIS_CH     = "signals:arbitrage"
REDIS_URL    = os.getenv("LOCAL_TEST", "redis://redis:6379/0")
THRESHOLD_BP = float(os.getenv("THRESHOLD_BP", 0))
LOG_MODE     = os.getenv("LOG_MODE", "spread").lower()   # silent / spread / orderbook / debug

EXCH_CLS = {
    "bybit":  ccxtpro.bybit,
    "okx":    ccxtpro.okx,
    "bitget": ccxtpro.bitget,
    "mexc":   ccxtpro.mexc,
}
FEE_BP = {
    "bybit":  int((0.0001 + 0.0001) * 1e4),   # taker+maker
    "okx":    int((0.00010 + 0.00008) * 1e4),
    "bitget": int((0.0010  + 0.0010) * 1e4),
    "mexc":   int((0.001   + 0.0)    * 1e4),
}
# ─────────────────────────────────────────────────────────────────────────

# ─── логгер + флаг «показывать стаканы» ─────────────────────────────────
logger.remove()
if LOG_MODE == "silent":
    logger.add(sys.stderr, level="WARNING"); SHOW_BOOKS = False
elif LOG_MODE == "spread":
    logger.add(sys.stderr, level="INFO");    SHOW_BOOKS = False
elif LOG_MODE == "orderbook":
    logger.add(sys.stderr, level="INFO");    SHOW_BOOKS = True
else:                                        # debug
    logger.add(sys.stderr, level="DEBUG");   SHOW_BOOKS = True
    logger.add(sys.stderr, backtrace=True, diagnose=True)
logger.info(f"LOG_MODE={LOG_MODE}, SHOW_BOOKS={SHOW_BOOKS}")

class WSListener:
    def __init__(self):
        self.books: dict[str, dict[str, dict]] = {}
        self.redis = aioredis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=True
        )
        self.records: list[dict] = []
        self.exchanges: dict[str, ccxtpro.Exchange] = {}

    # ─ helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def resolve_symbol(ex, pair: str):
        for cand in (
            pair, pair.replace("/", ""), pair.replace("/", "-"),
            pair.replace("/", "") + "_SPBL",
        ):
            if cand in ex.markets:
                return cand
        logger.warning(f"{ex.id}: {pair} not found – skip")
        return None

    @staticmethod
    def bp(bid, ask): return (bid - ask) / ask * 10_000

    # ─ подписка на биржу ────────────────────────────────────────────────
    async def _runner(self, ex_id):
        ex = EXCH_CLS[ex_id]({
            "enableRateLimit": True,
            "options": {"defaultType": "spot", "defaultSettle": "USDT"},
        })
        self.exchanges[ex_id] = ex
        await ex.load_markets()

        for p in PAIRS:
            a = self.resolve_symbol(ex, p)
            if a:
                asyncio.create_task(self._listen_pair(ex_id, ex, p, a))
        logger.success(f"{ex_id}: подписки запущены")

    async def _listen_pair(self, ex_id, ex, pair, alias):
        first = True
        limit = 5 if ex_id == "mexc" else 1
        while True:
            try:
                ob = await ex.watch_order_book(alias, limit)
                bid = ob["bids"][0][0] if ob["bids"] else None
                ask = ob["asks"][0][0] if ob["asks"] else None
                if bid and ask:
                    self.books.setdefault(pair, {})[ex_id] = {"bid": bid, "ask": ask}
                    if first and LOG_MODE == "debug":
                        logger.debug(f"{ex_id} {pair}: first {bid}/{ask}")
                        first = False
            except Exception as e:
                logger.warning(f"{ex_id} {pair}: {e}")
                await asyncio.sleep(2)

    # ─ расчёт спреда + вывод/Redis ─────────────────────────────────────
    async def _calc_loop(self):
        while True:
            await asyncio.sleep(POLL_SEC)
            if SHOW_BOOKS and self.books:
                for pair, d in self.books.items():
                    line = " | ".join(f"{ex}:{v['bid']:.5g}/{v['ask']:.5g}" for ex, v in d.items())
                    logger.info(f"{pair:<9} → {line}")

            for pair, d in self.books.items():
                if len(d) < 2: continue
                ask_ex, ask = min(d.items(), key=lambda x: x[1]["ask"])
                bid_ex, bid = max(d.items(), key=lambda x: x[1]["bid"])
                ask, bid = ask['ask'], bid['bid']
                spread_bp = self.bp(bid, ask)
                net_bp = spread_bp - (FEE_BP[ask_ex] + FEE_BP[bid_ex])

                self.records.append({
                    "ts": int(time.time()),
                    "pair": pair,
                    "buy_ex": ask_ex,  "buy_price": ask,
                    "sell_ex": bid_ex, "sell_price": bid,
                    "spread_bp": spread_bp,
                    "net_bp": net_bp,
                })

                if LOG_MODE in ("spread", "orderbook", "debug"):
                    logger.info(f"{pair:<9} {ask_ex}->{bid_ex} spread={spread_bp:.1f} net={net_bp:.1f} bp")

                if net_bp >= THRESHOLD_BP:
                    msg = {
                        "pair": pair, "buy_ex": ask_ex, "buy_price": ask,
                        "sell_ex": bid_ex, "sell_price": bid,
                        "spread_bp": spread_bp, "net_bp": net_bp,
                        "ts": int(time.time()),
                    }
                    await self.redis.publish(REDIS_CH, json.dumps(msg))
                    logger.success(f"signal → {msg}")

    # ─ запуск/остановка ────────────────────────────────────────────────
    async def start(self):
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try: loop.add_signal_handler(sig, stop.set)
            except NotImplementedError: pass

        tasks = [asyncio.create_task(self._runner(e)) for e in EXCH_CLS]
        tasks.append(asyncio.create_task(self._calc_loop()))
        try:
            await stop.wait()          # ждём Ctrl+C
        finally:
            # ─ корректно завершаем ──────────────────
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

            # закрываем redis
            await self.redis.close()

            # закрываем биржи, если ещё живы
            for ex in list(getattr(self, "exchanges", {}).values()):
                try:
                    await ex.close()
                except Exception:
                    pass

            # сохраняем Excel
            if getattr(self, "records", []):
                df = pd.DataFrame(self.records)
                pathlib.Path("data").mkdir(exist_ok=True)
                fname = time.strftime(
                    "data/spreads_%Y-%m-%d_%H-%M.xlsx", time.localtime()
                )
                df.to_excel(fname, index=False)
                logger.success(f"spread log saved → {fname} ({len(df)} rows)")
            else:
                logger.warning("no records collected – nothing to save")

# ─ entry ─
if __name__ == "__main__":
    asyncio.run(WSListener().start())
