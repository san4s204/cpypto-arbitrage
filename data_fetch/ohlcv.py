# src/data_fetch/ohlcv.py
import argparse, asyncio, re
import ccxt.async_support as ccxt        # << асинхронный вариант!
import pandas as pd
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from src.db.session import engine, Session
from src.db.models  import Base, OhlcvRaw
from src.config     import FETCH_CANDLE_MS
from data_fetch.exchange_factory import make_ex

# ──────────────────────────────────────────────── helpers
def chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def tf_to_ms(tf: str) -> int:
    """'30m' → 1_800_000  •  '2h' → 7_200_000"""
    num, unit = re.match(r"(\d+)([smhd])", tf).groups()
    num = int(num)
    if unit == "s":
        return num * 1_000
    if unit == "m":
        return num * 60_000
    if unit == "h":
        return num * 60 * 60_000
    if unit == "d":
        return num * 24 * 60 * 60_000
    raise ValueError("bad tf")

# ──────────────────────────────────────────────── main loader
async def fetch_ohlcv(exchange, symbol, tf, since_ms, until_ms):
    ohlcv, tf_ms = [], tf_to_ms(tf)
    while since_ms < until_ms:
        batch = await exchange.fetch_ohlcv(symbol, tf, since_ms, limit=200)
        if not batch:
            break
        ohlcv.extend(batch)
        since_ms = batch[-1][0] + tf_ms
        await asyncio.sleep(exchange.rateLimit / 1000)
    return ohlcv

async def job(exchange_name, symbol, days, tf):
    cls = getattr(ccxt, exchange_name)
    exchange = cls({"enableRateLimit": True})
    exchange = make_ex(exchange_name)

    now      = exchange.milliseconds()
    since_ms = now - days * 24 * 60 * 60 * 1000

    data = await fetch_ohlcv(exchange, symbol, tf, since_ms, now)
    await exchange.close()

    if not data:
        print(f"[{exchange_name}] no data for {symbol}")
        return

    # преобразуем в DataFrame
    df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "volume"])
    df["exchange"], df["symbol"],  = exchange_name, symbol

    # ─ insert into Postgres
    records = df.to_dict("records")
    with Session() as s:
        for chunk in chunks(records, 1000):
            stmt = insert(OhlcvRaw).values(chunk).on_conflict_do_nothing()
            s.execute(stmt)
        s.commit()
    print(f"[{exchange_name}] inserted {len(df)} rows for {symbol} ({tf})")

# ──────────────────────────────────────────────── CLI
def cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("symbol", help="e.g. BTC/USDT")
    parser.add_argument("exchange", choices=["bybit","okx"])
    parser.add_argument("--days", type=int, default=80, help="history depth")
    parser.add_argument("--tf", default="30m",          help="ccxt timeframe (30m, 1h, 2h …)")
    args = parser.parse_args()

    # ensure table exists
    Base.metadata.create_all(engine)

    asyncio.run(job(args.exchange, args.symbol, args.days))

if __name__ == "__main__":
    cli()
