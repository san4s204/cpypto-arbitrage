# src/data_fetch/fetch_markets.py
import pathlib, ccxt, pandas as pd
from functools import reduce

EXCHANGES = {
    "bybit":   ccxt.bybit,
    "okx":     ccxt.okx,
    "mexc":    ccxt.mexc,
    "bitget":  ccxt.bitget
}

def get_pairs(exchange_id: str) -> pd.DataFrame:
    """Вытащить все spot-пары c котировкой USDT для заданной биржи."""
    ex = EXCHANGES[exchange_id]()
    ex.load_markets()
    rows = []
    for m in ex.markets.values():
        if not m["spot"] or m["quote"] != "USDT":
            continue
        rows.append(
            {
                "symbol":     m["symbol"],          # BTC/USDT
                "base":       m["base"],
                "quote":      m["quote"],
                "exchange":   exchange_id,
                "listed_ts":  m.get("timestamp"),
                "tick_size":  m["precision"].get("price"),
                "min_qty":    m["limits"]["amount"]["min"],
            }
        )
    return pd.DataFrame(rows)

def main():
    # ─ 1. собираем DataFrame для каждой биржи
    dfs = {eid: get_pairs(eid) for eid in EXCHANGES}

    # ─ 2. ищем символы, общие для всех бирж  (inner-merge по 'symbol')
    inter = reduce(
        lambda left, right: pd.merge(
            left, right, on="symbol", suffixes=("", f"_{right.exchange.iloc[0]}")
        ),
        dfs.values(),
    )

    pathlib.Path("data").mkdir(exist_ok=True)
    inter.to_excel("data/all_pairs_raw.xlsx", index=False)
    print(f"✓ saved {len(inter)} common USDT pairs across Bybit, OKX, Binance, MEXC")

if __name__ == "__main__":
    main()
