from pathlib import Path
import subprocess, sys, argparse
import pandas as pd
from loguru import logger
from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parents[1]   # …/project_root
ENV  = ROOT / ".env"

# ── вызовы под-процессом, чтобы не тащить зависимости в память
def run(step_name: str, module: str, *args):
    logger.info(f"🏃 {step_name} …")
    cmd = [sys.executable, "-m", module, *map(str, args)]
    res = subprocess.run(cmd, cwd=ROOT)
    if res.returncode:
        logger.error(f"{step_name} failed (exit {res.returncode})")
        sys.exit(res.returncode)
    logger.success(f"{step_name} ✓")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--top", type=int, default=8, help="сколько пар оставить")
    ap.add_argument("--ohlcv", action="store_true", help="скачать OHLCV")
    ap.add_argument("--etl",   action="store_true", help="запустить ETL после OHLCV")
    ap.add_argument("--days",  type=int, default=80, help="глубина OHLCV (дни)")
    ap.add_argument("--tf",    default="30m", help="тайм-фрейм CCXT (по умолч. 30m)")
    args = ap.parse_args()

    # 1 ─ полные рынки
    run("Fetch markets",  "data_fetch.markets")

    # 2 ─ обогащение ликвидностью
    run("Decorate pairs", "data_fetch.decorate_pairs")

    # 3 ─ фильтр топ-пар
    run("Filter pairs",   "data_fetch.filters_pairs")

    # ── читаем pairs_top.xlsx и пишем .env
    top_xlsx = ROOT / "data" / "pairs_top.xlsx"
    if not top_xlsx.exists():
        logger.error("pairs_top.xlsx not found – step 3 failed?")
        sys.exit(1)

    pairs = pd.read_excel(top_xlsx)["symbol"].head(args.top).tolist()
    pairs_str = ",".join(pairs)
    env = dotenv_values(ENV)
    set_key(ENV, "PAIRS", pairs_str)
    logger.success(f".env обновлён → PAIRS={pairs_str}")

    # 4 ─ bulk_ohlcv (по желанию)
    if args.ohlcv:
        run("Bulk OHLCV",
            "data_fetch.bulk_ohlcv",
            f"--days={args.days}", f"--tf={args.tf}")

    # 5 ─ ETL (если выбран + после OHLCV)
    if args.etl:
        if not args.ohlcv:
            logger.warning("--etl требует --ohlcv; запускаю OHLCV сначала")
            run("Bulk OHLCV",
                "data_fetch.bulk_ohlcv",
                f"--days={args.days}", f"--tf={args.tf}")
        run("ETL", "processing.etl")

    logger.success("🏁 Pipeline completed")

if __name__ == "__main__":
    main()