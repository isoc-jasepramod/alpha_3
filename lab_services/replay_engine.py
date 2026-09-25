import os
import asyncio
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
import duckdb
from loguru import logger
import pyarrow as pa
import pyarrow.parquet as pq

from backend.core.redis_bus import RedisBus

class MarketReplayEngine:
    """
    Standalone Lab Service: Live Replay Engine.
    Queries Parquet files via DuckDB and streams them into Redis channel:simulated_ticks
    (or channel:market_ticks) at configurable speeds (1x to 10x).
    """

    def __init__(
        self,
        data_lake_dir: str = "data/lake",
        target_channel: Optional[str] = None,
        speed_multiplier: float = 2.0
    ):
        self.data_lake_dir = data_lake_dir
        self.speed = max(1.0, min(speed_multiplier, 10.0))
        self.redis_bus = RedisBus.from_config()
        self.target_channel = target_channel or self.redis_bus.simulated_channel
        self.running = False

    def check_or_create_sample_data(self, date_str: Optional[str] = None) -> str:
        """
        Creates a realistic simulated tick dataset for NIFTY spot + options
        if data lake is currently empty.
        """
        target_date = date_str or datetime.now().strftime("%Y-%m-%d")
        partition_dir = os.path.join(self.data_lake_dir, f"date={target_date}")
        os.makedirs(partition_dir, exist_ok=True)
        sample_file = os.path.join(partition_dir, "simulated_morning_session.parquet")

        if os.path.exists(sample_file):
            return sample_file

        logger.info(f"Generating realistic simulation tick dataset in {sample_file}...")
        ticks = []
        base_time = datetime.strptime(f"{target_date} 09:15:00", "%Y-%m-%d %H:%M:%S")
        
        from backend.core.instrument_manager import InstrumentManager
        mgr = InstrumentManager()
        asyncio.run(mgr.sync_master())
        wings = mgr.get_atm_and_wings("NIFTY", 25000.0)
        ce_meta = wings["by_strike"].get(25000.0, {}).get("CE", {})
        token_ce = str(ce_meta.get("token", "57796"))
        symbol_ce = ce_meta.get("symbol", "NIFTY25000CE")
        
        # Spot starts at 25,000 and expands upwards
        spot_p = 25000.0
        atm_ce_p = 120.0
        atm_pe_p = 115.0
        oi_ce = 2500000
        oi_pe = 2200000
        vol_ce = 10000
        vol_pe = 8000

        for sec in range(0, 3600, 2): # 1 hour of ticks every 2 seconds
            cur_time = base_time + timedelta(seconds=sec)
            ts = int(cur_time.timestamp())

            # Momentum expansion: Spot climbs steadily
            spot_p += (0.8 if sec < 900 else 1.5 if sec < 1800 else -0.3)
            # CE options surge
            atm_ce_p += (0.5 if sec < 900 else 1.1 if sec < 1800 else -0.2)
            # CE OI drops significantly between 09:20 and 09:25 (Short Covering Capitulation)
            if 300 <= sec <= 600:
                oi_ce -= 2500 # total drop ~375,000 (~15%)
                vol_ce += 1500
            else:
                vol_ce += 100

            # Spot tick
            ticks.append({
                "token": "99926000",
                "exchange": "nse_cm",
                "ltp": round(spot_p, 2),
                "volume": 0.0,
                "open_interest": 0.0,
                "open": 25000.0,
                "high": max(spot_p, 25000.0),
                "low": min(spot_p, 24990.0),
                "close": round(spot_p, 2),
                "exchange_timestamp": ts,
                "received_at": cur_time.isoformat()
            })

            # ATM CE Option tick
            ticks.append({
                "token": token_ce,
                "exchange": "nfo_fo",
                "ltp": round(atm_ce_p, 2),
                "volume": float(vol_ce),
                "open_interest": float(oi_ce),
                "open": 120.0,
                "high": max(atm_ce_p, 120.0),
                "low": min(atm_ce_p, 118.0),
                "close": round(atm_ce_p, 2),
                "exchange_timestamp": ts,
                "received_at": cur_time.isoformat()
            })

        table = pa.Table.from_pylist(ticks)
        pq.write_table(table, sample_file, compression="snappy")
        logger.info(f"Sample dataset generated ({len(ticks)} ticks).")
        return sample_file

    async def replay(self, parquet_path: Optional[str] = None, speed: Optional[float] = None, target_channel: Optional[str] = None):
        """
        Streams ticks from parquet into Redis Pub/Sub at configured speed.
        """
        if speed:
            self.speed = max(1.0, min(speed, 10.0))
        channel = target_channel or self.target_channel

        if not parquet_path or not os.path.exists(parquet_path):
            parquet_path = self.check_or_create_sample_data()

        logger.info(f"🚀 Starting Market Replay from {parquet_path} at {self.speed}x speed on '{channel}'...")
        await self.redis_bus.connect()
        self.running = True

        # Query sorted ticks with DuckDB
        conn = duckdb.connect()
        query = f"SELECT * FROM read_parquet('{parquet_path}') ORDER BY exchange_timestamp ASC"
        df = conn.execute(query).df()
        conn.close()

        logger.info(f"Loaded {len(df)} ticks into DuckDB replay buffer.")
        records = df.to_dict(orient="records")

        if not records:
            logger.warning("No records to replay.")
            return

        last_sim_ts = None
        for r in records:
            if not self.running:
                break

            current_sim_ts = r.get("exchange_timestamp", 0)
            if last_sim_ts is not None and current_sim_ts > last_sim_ts:
                delta_sec = (current_sim_ts - last_sim_ts) / self.speed
                # Cap sleep to max 1.0s to avoid long pauses
                sleep_time = min(max(delta_sec, 0.005), 1.0)
                await asyncio.sleep(sleep_time)

            last_sim_ts = current_sim_ts

            # Clean record for Redis payload
            tick_payload = {
                "token": str(r["token"]),
                "exchange": str(r["exchange"]),
                "ltp": float(r["ltp"]),
                "volume": float(r["volume"]),
                "open_interest": float(r["open_interest"]),
                "open": float(r.get("open", 0.0)),
                "high": float(r.get("high", 0.0)),
                "low": float(r.get("low", 0.0)),
                "close": float(r.get("close", 0.0)),
                "exchange_timestamp": int(r["exchange_timestamp"]),
                "simulated": True
            }
            await self.redis_bus.publish_tick(tick_payload, channel=channel)

        logger.info("Market Replay finished.")

    def stop(self):
        self.running = False

if __name__ == "__main__":
    replayer = MarketReplayEngine(speed_multiplier=5.0)
    asyncio.run(replayer.replay())
