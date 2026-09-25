import asyncio
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.database.db import AsyncSessionLocal
from backend.database.models import Signal, DailyJournal
from sqlalchemy import select, delete

async def reset():
    async with AsyncSessionLocal() as session:
        # Delete today's signals that triggered prematurely
        stmt = delete(Signal).where(Signal.signal_id.like("SIG-20260921%"))
        res = await session.execute(stmt)
        print(f"Deleted {res.rowcount} premature morning signals from database.")

        # Clear DailyJournal for today
        stmt_j = delete(DailyJournal)
        res_j = await session.execute(stmt_j)
        print(f"Reset DailyJournal table.")
        await session.commit()
    print("Database reset completed.")

if __name__ == "__main__":
    asyncio.run(reset())
