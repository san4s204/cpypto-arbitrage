"""
Обогащает raw-список пар ликвидностью 4 бирж и сохраняет enriched-файл.
"""

import time, pathlib, ccxt, pandas as pd
from tqdm import tqdm

RAW_XLSX = "data/all_pairs_raw.xlsx"
ENRICHED = "data/all_pairs_enriched.xlsx"

# ──────────── CCXT sync-клиенты
exchs = {
    "bybit":   ccxt.bybit(  {"enableRateLimit": True}),
    "okx":     ccxt.okx(    {"enableRateLimit": True}),
    "mexc":    ccxt.mexc(   {"enableRateLimit": True}),
    "bitget":  ccxt.bitget(  {"enableRateLimit": True}),
}

# ──────────── helpers
def ticker_safe(ex, sym):
    try:
        return ex.fetch_ticker(sym)
    except Exception as e:
        print(f"[warn] ticker {ex.id} {sym}: {e}")
        return None

def depth_safe(ex, sym):
    try:
        ob = ex.fetch_order_book(sym, 1)
        if not ob["bids"]:
            return 0.0
        p, q = map(float, ob["bids"][0][:2])
        return p * q
    except Exception as e:
        print(f"[warn] depth  {ex.id} {sym}: {e}")
        return 0.0

# ──────────── main
def main():
    pathlib.Path("data").mkdir(exist_ok=True)
    df = pd.read_excel(RAW_XLSX)

    # подготовим словари для «сырых» метрик
    vols, deps = {ex: [] for ex in exchs}, {ex: [] for ex in exchs}

    for _, row in tqdm(df.iterrows(), total=len(df), ncols=90, desc="enrich pairs"):
        sym_std = row["symbol"]          # BTC/USDT

        #   BYBIT  : BTCUSDT
        #   OKX    : BTC-USDT
        #   BINANCE/MEXC : BTC/USDT
        symbols = {
            "bybit":   sym_std.replace("/", ""),
            "okx":     sym_std.replace("/", "-"),
            "bitget": sym_std,
            "mexc":    sym_std,
        }

        for ex_id, ex in exchs.items():
            t = ticker_safe(ex, symbols[ex_id])
            vols[ex_id].append(float(t["quoteVolume"]) if t else 0.0)

            deps[ex_id].append(depth_safe(ex, symbols[ex_id]))

        time.sleep(0.05)

    # ─ добавляем «сырые» колонки
    for ex_id in exchs:
        df[f"volume_{ex_id}"] = vols[ex_id]
        df[f"depth_{ex_id}"]  = deps[ex_id]

    # ─ минимумы и источник биржи-«бутылочного горлышка»
    vol_cols  = [f"volume_{ex}" for ex in exchs]
    depth_cols = [f"depth_{ex}" for ex in exchs]

    df["volume_24h"] = df[vol_cols].min(axis=1)
    df["depth"]      = df[depth_cols].mean(axis=1)

    df["vol_src"]   = df[vol_cols].idxmin(axis=1).str.replace("volume_", "")
    df["depth_src"] = df[depth_cols].idxmin(axis=1).str.replace("depth_", "")

    df.to_excel(ENRICHED, index=False)
    print(f"✓ enriched Excel saved: {ENRICHED}   ({len(df)} rows)")

if __name__ == "__main__":
    main()
