import os
import os
import sys

# Ensure the project root is importable when this script is run directly
# (e.g. `python lab_services/tick_recorder.py`), matching the other lab_services tools.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import json
import asyncio
import time
from datetime import datetime, timezone, date
from typing import List, Dict, Any, Set
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

    Also maintains a token -> instrument metadata sidecar (token_meta.json) per
    day partition so a future replay knows what each numeric token represented
    (NIFTY/SENSEX, strike, CE/PE, offset, lot_size, expiry) — even as ATM migrates.
    The metadata is resolved from the AngelOne instrument master across a wide
    strike band so all traded tokens are covered.
    """

    def __init__(self, data_lake_dir: str = "data/lake", flush_interval_sec: int = 60):
        self.data_lake_dir = data_lake_dir
        self.flush_interval = flush_interval_sec
        self.buffer: List[Dict[str, Any]] = []
        self.redis_bus = RedisBus.from_config()
        self.running = False
        self._seen_tokens: Set[str] = set()
        self._meta_written_for: Dict[str, Any] = {}
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
                tok = str(tick.get("token", ""))
                if tok:
                    self._seen_tokens.add(tok)
        except asyncio.CancelledError:
            pass
        finally:
            flush_task.cancel()
            await self._flush_buffer_to_parquet()
            await self._write_token_metadata()
            await self.redis_bus.close()
            logger.info("Tick Recorder stopped.")

    async def _write_token_metadata(self):
        """
        Resolves and persists a token -> instrument metadata map for every token
        seen today, using the AngelOne instrument master. Written as token_meta.json
        alongside the day's Parquet files so replay can attach meta to each tick.
        """
        if not self._seen_tokens:
            return
        try:
            today_str = date.today().strftime("%Y-%m-%d")
            partition_dir = os.path.join(self.data_lake_dir, f"date={today_str}")
            os.makedirs(partition_dir, exist_ok=True)
            meta_path = os.path.join(partition_dir, "token_meta.json")

            loop = asyncio.get_running_loop()
            meta_map = await loop.run_in_executor(None, self._resolve_token_meta, set(self._seen_tokens))

            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta_map, f, indent=2)
            logger.info(f"🗂️  Wrote token metadata for {len(meta_map)} tokens to {meta_path}")
        except Exception as e:
            logger.error(f"Error writing token metadata sidecar: {e}")

    def _resolve_token_meta(self, tokens: Set[str]) -> Dict[str, Any]:
        """Builds token -> meta using the InstrumentManager master (runs in thread pool)."""
        meta_map: Dict[str, Any] = {}
        # Spot tokens are always known
        spot_meta = {
            "99926000": {"name": "NIFTY", "is_spot": True, "exchange": "nse_cm"},
            "26000": {"name": "NIFTY", "is_spot": True, "exchange": "nse_cm"},
            "99919000": {"name": "SENSEX", "is_spot": True, "exchange": "bse_cm"},
        }
        for t, m in spot_meta.items():
            if t in tokens:
                meta_map[t] = m

        try:
            from backend.core.instrument_manager import InstrumentManager
            mgr = InstrumentManager()
            # Load cached master (already synced by the live engine today)
            mgr._load_from_cache()
            # Index every OPTIDX contract for NIFTY & SENSEX by token
            for symbol in ("NIFTY", "SENSEX"):
                for c in mgr.contracts_by_symbol.get(symbol, []):
                    tok = str(c.get("token", ""))
                    if tok in tokens:
                        raw_strike = float(c.get("strike", 0.0))
                        strike_val = raw_strike / 100.0 if raw_strike > 100000 else raw_strike
                        sym_code = c.get("symbol", "")
                        opt_type = "CE" if sym_code.endswith("CE") else "PE" if sym_code.endswith("PE") else None
                        meta_map[tok] = {
                            "name": symbol,
                            "symbol": sym_code,
                            "strike": strike_val,
                            "option_type": opt_type,
                            "expiry": c.get("expiry", ""),
                            "lot_size": int(c.get("lotsize", 0)) if str(c.get("lotsize", "")).isdigit() else 0,
                            "exchange": c.get("exch_seg", ""),
                            "is_spot": False
                        }
        except Exception as e:
            logger.warning(f"Could not resolve full option metadata from master: {e}")

        # Any token still unresolved gets a placeholder so replay can flag it
        for t in tokens:
            if t not in meta_map:
                meta_map[t] = {"name": "UNKNOWN", "is_spot": False, "unresolved": True}
        return meta_map

    async def _flush_loop(self):
        while self.running:
            await asyncio.sleep(self.flush_interval)
            await self._flush_buffer_to_parquet()
            # Rewrite the token metadata sidecar on every flush so it survives a
            # hard kill (e.g. taskkill from stop_app.bat) — replay needs this file.
            await self._write_token_metadata()

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
            # Normalize schema fields — capture the FULL SnapQuote payload so replay
            # can faithfully reconstruct order flow (buy/sell qty), true tick volume
            # (last_traded_qty), and OI context. Nothing the parser produces is dropped.
            records = []
            for t in ticks:
                records.append({
                    "token": str(t.get("token", "")),
                    "exchange": str(t.get("exchange", "")),
                    "mode": int(t.get("mode", 0)),
                    "ltp": float(t.get("ltp", 0.0)),
                    "last_traded_qty": float(t.get("last_traded_qty", 0.0)),
                    "avg_traded_price": float(t.get("avg_traded_price", 0.0)),
                    "volume": float(t.get("volume", 0.0)),
                    "total_buy_qty": float(t.get("total_buy_qty", 0.0)),
                    "total_sell_qty": float(t.get("total_sell_qty", 0.0)),
                    "open_interest": float(t.get("open_interest", 0.0)),
                    "oi_day_high": float(t.get("oi_day_high", 0.0)),
                    "oi_day_low": float(t.get("oi_day_low", 0.0)),
                    "open": float(t.get("open", 0.0)),
                    "high": float(t.get("high", 0.0)),
                    "low": float(t.get("low", 0.0)),
                    "close": float(t.get("close", 0.0)),
                    "last_traded_timestamp": int(t.get("last_traded_timestamp", 0)),
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
