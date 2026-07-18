# processing/etl.py
import pandas as pd, sqlalchemy as sa, tqdm
from src.db.session import Session, engine
from src.db.models  import OhlcvRaw, OhlcvClean, Base

# ───────── комиссии (decimals, 0.0001 = 0.01 %)
MAKER = {"bybit": 0.0001,  "okx": 0.00008, "bitget":0.0010,  "mexc": 0.0}
TAKER = {"bybit": 0.0001,  "okx": 0.00010, "bitget":0.0010,  "mexc": 0.001}

EXCHS = ["bybit", "okx", "bitget", "mexc"]     # порядок колонок

def main():
    Base.metadata.create_all(engine)

    with Session() as s:
        raw = pd.read_sql(sa.select(OhlcvRaw), s.bind)

    # 1. mid-price
    raw["mid"] = (raw["high"] + raw["low"]) / 2

    # 2. pivot таблица: по колонке на биржу
    piv = raw.pivot_table(index=["symbol", "ts"],
                          columns="exchange",
                          values="mid").reset_index()
    piv.columns.name = None

    # 3. оставляем строки, где есть все 4 биржи
    piv = piv.dropna(subset=EXCHS)

    # 4. определяем «дешёвую» и «дорогую» стороны
    piv["buy_ex"]  = piv[EXCHS].idxmin(axis=1)
    piv["sell_ex"] = piv[EXCHS].idxmax(axis=1)

    piv["buy_mid"]  = piv.apply(lambda r: r[r["buy_ex"]],  axis=1)
    piv["sell_mid"] = piv.apply(lambda r: r[r["sell_ex"]], axis=1)
    # 5. комиссии maker–taker
    fee_total = (
        piv["buy_mid"]  * piv["buy_ex"].map(TAKER) +
        piv["sell_mid"] * piv["sell_ex"].map(MAKER)
    )

    piv["spread"]      = piv["sell_mid"] - piv["buy_mid"]
    piv["net_spread"]  = piv["spread"] - fee_total

    # 6. пишем в БД
    cols = ["symbol", "ts",
            "buy_ex", "sell_ex", "buy_mid", "sell_mid",
            "spread", "net_spread"]
    recs = piv[cols].to_dict("records")

    with Session() as s:
        for chunk in tqdm.tqdm([recs[i:i+1000] for i in range(0, len(recs), 1000)],
                               desc="insert clean"):
            s.execute(sa.dialects.postgresql.insert(OhlcvClean)
                      .values(chunk)
                      .on_conflict_do_nothing())
        s.commit()

    print(f"✓ inserted {len(piv)} rows into ohlcv_clean")

if __name__ == "__main__":
    main()