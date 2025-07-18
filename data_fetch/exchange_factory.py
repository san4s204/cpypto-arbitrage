import ccxt.async_support as ccxt

def make_ex(exchange_id: str):
    opts = {
        "enableRateLimit": True,
        "timeout": 30_000,            # 30 c вместо дефолтных 10 c
        "options": {"defaultType": "spot"},   # ← важное
    }
    cls = getattr(ccxt, exchange_id)
    return cls(opts)