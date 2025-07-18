"""
Скачивает OHLCV (spot) за N-дней для всех пар из data/pairs_top.xlsx
на Bybit, OKX, Bitget и MEXC, пишет в таблицу ohlcv_raw.

Примеры
    python -m src.data_fetch.bulk_ohlcv           # 30-min, 80 дней
    python -m src.data_fetch.bulk_ohlcv --tf 5m   # 5-min
"""
import asyncio, argparse, pandas as pd
from tqdm.asyncio import tqdm_asyncio

from data_fetch.ohlcv           import job          # корутина-загрузчик
from src.db.session                 import engine
from src.db.models                  import Base

PAIRS_XLSX = "data/pairs_top.xlsx"
SEM        = asyncio.Semaphore(3)    # ≤ 3 одновременных запросов/биржу

# ─────────── helpers
async def limited_job(ex_id, symbol, days, tf):
    async with SEM:
        await job(ex_id, symbol, days, tf=tf)

async def main(days: int, tf: str):
    df = pd.read_excel(PAIRS_XLSX)
    ex_ids = ["bybit", "okx", "bitget", "mexc"]

    tasks = [
        limited_job(ex_id, sym, days, tf)
        for sym in df["symbol"]
        for ex_id in ex_ids
    ]

    await tqdm_asyncio.gather(*tasks, desc=f"OHLCV {tf} download")

# ─────────── CLI
if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=80,  help="глубина, дней (default 80)")
    p.add_argument("--tf",   default="30m",         help="тайм-фрейм CCXT (default 30m)")
    args = p.parse_args()

    Base.metadata.create_all(engine)
    asyncio.run(main(args.days, args.tf))
