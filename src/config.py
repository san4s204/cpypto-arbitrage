from dotenv import load_dotenv
import os

load_dotenv()

DB_URL          = os.getenv("DB_URL")
PAIRS_CSV       = os.getenv("PAIRS_CSV", "data/pairs.csv")
FETCH_CANDLE_MS = 2 * 60 * 60 * 1000   # 2h в миллисекундах
BYBIT           = {"key": os.getenv("BYBIT_API_KEY"), "secret": os.getenv("BYBIT_API_SECRET")}
OKX             = {"key": os.getenv("OKX_API_KEY"),   "secret": os.getenv("OKX_API_SECRET")}
