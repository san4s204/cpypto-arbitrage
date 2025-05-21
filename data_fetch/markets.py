import ccxt
import pandas as pd
from src.config import PAIRS_CSV
from src.utils.logging import log

def fetch_pairs() -> pd.DataFrame:
    bybit = ccxt.bybit()
    okx   = ccxt.okx()
    bybit.load_markets()
    okx.load_markets()

    usdt_bybit = {s for s,m in bybit.markets.items() if m['quote'] == 'USDT' and m['spot']}
    usdt_okx   = {s.replace('-', '/') for s,m in okx.markets.items() if m['quote'] == 'USDT' and m['spot']}

    shared = usdt_bybit & usdt_okx
    df = pd.DataFrame(sorted(shared), columns=['symbol'])
    df[['base','quote']] = df['symbol'].str.split('/', expand=True)
    df.to_csv(PAIRS_CSV, index=False)
    log.info(f"Saved {len(df)} common USDT pairs to {PAIRS_CSV}")
    return df

if __name__ == "__main__":
    fetch_pairs()
