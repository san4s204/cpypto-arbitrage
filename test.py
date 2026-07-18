import asyncio, redis.asyncio as r

async def ping(url):
    try:
        client = r.from_url(url, encoding="utf-8", decode_responses=True)
        print("PING →", await client.ping())
    except Exception as e:
        print("FAIL:", e)

asyncio.run(ping("redis://127.0.0.1:6379/0"))