import asyncio, sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from backend.database.db import AsyncSessionLocal
from backend.database.models import Signal, DailyJournal
from sqlalchemy import select

async def check():
    async with AsyncSessionLocal() as s:
        res = await s.execute(select(Signal))
        sigs = res.scalars().all()
        print('Total Signals in DB:', len(sigs))
        for sig in sigs:
            print(sig.created_at, sig.signal_id, sig.strategy, sig.status, sig.theoretical_pnl)
        res_j = await s.execute(select(DailyJournal))
        js = res_j.scalars().all()
        print('Journals:', [j.to_dict() for j in js])

if __name__ == "__main__":
    asyncio.run(check())
