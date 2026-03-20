import asyncio, aiohttp
from datetime import datetime, timezone

async def main():
    now = datetime.now(timezone.utc)
    async with aiohttp.ClientSession() as sess:
        async with sess.get('https://gamma-api.polymarket.com/events',
                           params={'tag_slug': 'crypto', 'active': 'true', 'limit': 100}) as r:
            events = await r.json()

        future_by_fees = {}
        for ev in events:
            for mkt in ev.get('markets', []):
                end = mkt.get('endDate','')
                if not end:
                    continue
                try:
                    ed = datetime.fromisoformat(end.replace('Z','+00:00'))
                    if ed > now:
                        key = str(mkt.get('feesEnabled'))
                        q = mkt.get('question', mkt.get('title',''))[:55]
                        future_by_fees.setdefault(key, []).append((end[:16], q))
                except:
                    pass

        for fees_val, items in future_by_fees.items():
            print(f'feesEnabled={fees_val}: {len(items)} future markets')
            for end, q in items[:5]:
                print(f'  {end}  {q}')
            print()

asyncio.run(main())
