import os
import asyncio
import time
from datetime import datetime, timezone, date
from typing import List, Dict, Any
import pyarrow as pa
import pyarrow.parquet as pq
from loguru import logger
from backend.core.redis_bus import RedisBus

class TickRecorder:
    """
    Standalone Lab Service: Passive Tick Recorder.
    Subscribes to Redis channel:market_ticks.
    Buffers incoming raw ticks in memory and flushes them to partitioned
    Apache Parquet files every 60 seconds without blocking the live engine.
    """

    def __init__(self, data_lake_dir: str = "data/lake", flush_interval_sec: int = 60):
        self.data_lake_dir = data_lake_dir
        self.flush_interval = flush_interval_sec
        self.buffer: List[Dict[str, Any]] = []
        self.redis_bus = RedisBus.from_config()
        self.running = False
        os.makedirs(self.data_lake_dir, exist_ok=True)

    async def start(self):
        self.running = True
        logger.info(f"Tick Recorder started. Flush interval: {self.flush_interval}s. Target: {self.data_lake_dir}")
        await self.redis_bus.connect()

        # Start flush loop
        flush_task = asyncio.create_task(self._flush_loop())

        try:
            async for tick in self.redis_bus.subscribe(self.redis_bus.market_channel):
                if not self.running:
                    break
                self.buffer.append(tick)
        except asyncio.CancelledError:
            pass
        finally:
            flush_task.cancel()
            await self._flush_buffer_to_parquet()
            await self.redis_bus.close()
            logger.info("Tick Recorder stopped.")

    async def _flush_loop(self):
        while self.running:
            await asyncio.sleep(self.flush_interval)
            await self._flush_buffer_to_parquet()

    async def _flush_buffer_to_parquet(self):
        if not self.buffer:
            return

        # Snapshot and clear buffer
        ticks_to_flush = list(self.buffer)
        self.buffer.clear()

        # Run file I/O in thread pool to avoid blocking asyncio loop
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._write_parquet, ticks_to_flush)

    def _write_parquet(self, ticks: List[Dict[str, Any]]):
        try:
            today_str = date.today().strftime("%Y-%m-%d")
            partition_dir = os.path.join(self.data_lake_dir, f"date={today_str}")
            os.makedirs(partition_dir, exist_ok=True)

            filename = f"ticks_{int(time.time())}.parquet"
            filepath = os.path.join(partition_dir, filename)

            # Convert to PyArrow Table
            # Normalize schema fields
            records = []
            for t in ticks:
                records.append({
                    "token": str(t.get("token", "")),
                    "exchange": str(t.get("exchange", "")),
                    "ltp": float(t.get("ltp", 0.0)),
                    "volume": float(t.get("volume", 0.0)),
                    "open_interest": float(t.get("open_interest", 0.0)),
                    "open": float(t.get("open", 0.0)),
                    "high": float(t.get("high", 0.0)),
                    "low": float(t.get("low", 0.0)),
                    "close": float(t.get("close", 0.0)),
                    "exchange_timestamp": int(t.get("exchange_timestamp", 0)),
                    "received_at": str(t.get("received_at", datetime.now(timezone.utc).isoformat()))
                })

            table = pa.Table.from_pylist(records)
            pq.write_table(table, filepath, compression="snappy")
            logger.info(f"💾 Flushed {len(ticks)} ticks to Parquet: {filepath}")

        except Exception as e:
            logger.error(f"Error writing Parquet partition: {e}")

if __name__ == "__main__":
    recorder = TickRecorder()
    asyncio.run(recorder.start())
