import pandas as pd, pathlib

SRC       = "data/all_pairs_enriched.xlsx"
DEST      = "data/pairs_top.xlsx"
VOL_MIN   = 200000      # USDT
DEPTH_MIN = 2000          # USDT

def main():
    df = pd.read_excel(SRC)

    top = df[(df["volume_24h"] >= VOL_MIN) &
             (df["depth"]      >= DEPTH_MIN)].copy()

    # сортируем по объёму для красоты
    top.sort_values("volume_24h", ascending=False, inplace=True)

    pathlib.Path("data").mkdir(exist_ok=True)
    top.to_excel(DEST, index=False)
    print(f"✓ saved {DEST}  —  {len(top)} pairs (из {len(df)})")

    # небольшая сводка
    print(top[["symbol","volume_24h","depth","vol_src","depth_src"]].head(10))

if __name__ == "__main__":
    main()
