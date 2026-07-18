# realtime/signal_consumer.py
"""
Подписывается на канал signals:arbitrage и выводит уведомления в консоль.
Если задать TELEGRAM_TOKEN + TELEGRAM_CHAT в .env — шлёт алерты в Telegram.
"""
import asyncio, json, os, sys, time
from loguru import logger
import redis.asyncio as aioredis
import httpx
from dotenv import load_dotenv

load_dotenv()

REDIS_URL  = os.getenv("LOCAL_TEST", "redis://redis:6379/0")
CHANNEL    = "signals:arbitrage"
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN")      # optional
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT")       # optional

CHAT_IDS = [c for c in TG_CHAT_ID.split(",") if c]

logger.remove()
logger.add(sys.stderr, level="INFO")

async def send_telegram(text: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as cli:
        for chat in CHAT_IDS:
            try:
                await cli.post(url, json={"chat_id": chat, "text": text})
            except Exception as e:
                logger.error(f"Telegram send to {chat} failed: {e}")

def fmt_msg(msg: dict) -> str:
    return (f"{msg['pair']}  {msg['buy_ex']}→{msg['sell_ex']} | "
            f"{msg['net_bp']:.1f} bp  ({msg['buy_price']} → {msg['sell_price']})")

async def main():
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    pubsub = redis.pubsub()
    await pubsub.subscribe(CHANNEL)
    logger.success(f"subscribed to {CHANNEL}")

    async for raw in pubsub.listen():
        if raw["type"] != "message":
            continue
        try:
            data = json.loads(raw["data"])
        except json.JSONDecodeError:
            logger.warning(f"bad json: {raw['data'][:50]}")
            continue
        text = fmt_msg(data)
        logger.success(text)
        await send_telegram(text)

if __name__ == "__main__":
    print(TG_CHAT_ID)
    asyncio.run(main())
