from dotenv import load_dotenv
import os

load_dotenv(override=True)

DB_URL          = os.getenv("DB_URL")
FETCH_CANDLE_MS = 30 * 60 * 1000   # 30 min в миллисекундах
BYBIT           = {"key": os.getenv("BYBIT_API_KEY"), "secret": os.getenv("BYBIT_API_SECRET")}
OKX             = {"key": os.getenv("OKX_API_KEY"),   "secret": os.getenv("OKX_API_SECRET")}


PAIRS_RAW = os.getenv("PAIRS_RAW",  "")
PAIRS: list[str] = [p.strip().upper() for p in PAIRS_RAW.split(",") if p.strip()]

PAIR_MAP: dict[str, list[str]] = {
    "bybit": [s.replace("/", "")   for s in PAIRS],      # BTC/USDT → BTCUSDT
    "okx":   [s.replace("/", "-")  for s in PAIRS],      # BTC/USDT → BTC-USDT
    "htx":   PAIRS,                                      # классический вид
    "mexc":  PAIRS,
}

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")