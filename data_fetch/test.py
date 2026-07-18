import pandas as pd
df = pd.read_csv("data/all_pairs_enriched.csv")
row = df[df.symbol == "SOL/USDT"].iloc[0]

print("Bybit  volume:", row.volume_bybit, "   depth:", row.depth_bybit)
print("OKX    volume:", row.volume_okx,   "   depth:", row.depth_okx)
print("min(volume_24h):", row.volume_24h)
print("min(depth):     ", row.depth)