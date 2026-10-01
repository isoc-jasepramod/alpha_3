import os
import sys
# Allow running standalone (python lab_services/tick_recorder.py) without a 'backend' import
# error — insert the project root on the path before importing backend.*.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

    # Known index spot tokens (no order book / not options).
    SPOT_TOKENS = {
        "99926000": {"name": "NIFTY", "is_spot": True, "exchange": "nse_cm"},
        "26000": {"name": "NIFTY", "is_spot": True, "exchange": "nse_cm"},
        "99919000": {"name": "SENSEX", "is_spot": True, "exchange": "bse_cm"},
    }

    def __init__(self, data_lake_dir: str = "data/lake", flush_interval_sec: int = 60):
        self.data_lake_dir = data_lake_dir
        self.flush_interval = flush_interval_sec
        self.buffer: List[Dict[str, Any]] = []
        self.redis_bus = RedisBus.from_config()
        self.running = False
        os.makedirs(self.data_lake_dir, exist_ok=True)
        # token -> metadata (strike/option_type/name/lot_size/is_spot). Built from the
        # instrument master so the FlowEngine v2 backtest can map recorded tokens to
        # contracts. Without this sidecar the per-tick numbers are un-interpretable.
        self._token_meta: Dict[str, Dict[str, Any]] = {}
        self._seen_tokens: set = set()
        self._master_index: Dict[str, Dict[str, Any]] = {}

    def _build_master_index(self):
        """Load the instrument master once and build a token -> contract-metadata reverse index."""
        try:
            from backend.core.instrument_manager import InstrumentManager
            mgr = InstrumentManager()
            # Load from cache if present; the live app refreshes it daily.
            mgr._load_from_cache()
            for item in getattr(mgr, "instruments", []) or []:
                # Only NIFTY/SENSEX index options — the only option tokens we ever subscribe.
                if item.get("name") not in ("NIFTY", "SENSEX"):
                    continue
                if item.get("instrumenttype") not in ("OPTIDX", "OPTFUT", "FUTIDX"):
                    continue
                tok = str(item.get("token", ""))
                if not tok:
                    continue
                sym = item.get("symbol", "")
                opt_type = "CE" if sym.endswith("CE") else ("PE" if sym.endswith("PE") else None)
                raw_strike = 0.0
                try:
                    raw_strike = float(item.get("strike", 0.0))
                except (TypeError, ValueError):
                    raw_strike = 0.0
                strike_val = raw_strike / 100.0 if raw_strike > 100000 else raw_strike
                self._master_index[tok] = {
                    "name": item.get("name", ""),
                    "symbol": sym,
                    "strike": strike_val,
                    "option_type": opt_type,
                    "expiry": item.get("expiry", ""),
                    "lot_size": int(float(item.get("lotsize", 0) or 0)),
                    "instrumenttype": item.get("instrumenttype", ""),
                    "is_spot": False,
                }
            logger.info(f"Tick Recorder: indexed {len(self._master_index)} instruments for token metadata.")
        except Exception as e:
            logger.warning(f"Tick Recorder: could not build master index ({e}); metadata sidecar will be sparse.")

    def _resolve_meta(self, token: str) -> Dict[str, Any]:
        """Resolve a token to its metadata (spot tokens first, then the master index)."""
        if token in self.SPOT_TOKENS:
            return dict(self.SPOT_TOKENS[token])
        m = self._master_index.get(token)
        if m:
            return dict(m)
        return {"name": "", "is_spot": False, "unresolved": True}

    def _write_token_metadata(self):
        """Write/refresh token_meta.json for all tokens seen so far (survives hard kill)."""
        try:
            import json
            today_str = date.today().strftime("%Y-%m-%d")
            partition_dir = os.path.join(self.data_lake_dir, f"date={today_str}")
            os.makedirs(partition_dir, exist_ok=True)
            for tok in self._seen_tokens:
                if tok not in self._token_meta:
                    self._token_meta[tok] = self._resolve_meta(tok)
            with open(os.path.join(partition_dir, "token_meta.json"), "w") as f:
                json.dump(self._token_meta, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"Tick Recorder: failed to write token_meta.json ({e}).")

    async def start(self):
        self.running = True
        logger.info(f"Tick Recorder started. Flush interval: {self.flush_interval}s. Target: {self.data_lake_dir}")
        self._build_master_index()
        await self.redis_bus.connect()

        # Start flush loop
        flush_task = asyncio.create_task(self._flush_loop())

        try:
            async for tick in self.redis_bus.subscribe(self.redis_bus.market_channel):
                if not self.running:
                    break
                tok = str(tick.get("token", ""))
                if tok:
                    self._seen_tokens.add(tok)
                self.buffer.append(tick)
        except asyncio.CancelledError:
            pass
        finally:
            flush_task.cancel()
            await self._flush_buffer_to_parquet()
            self._write_token_metadata()
            await self.redis_bus.close()
            logger.info("Tick Recorder stopped.")

    async def _flush_loop(self):
        while self.running:
            await asyncio.sleep(self.flush_interval)
            await self._flush_buffer_to_parquet()
            # Rewrite the metadata sidecar each flush so it survives a hard kill (taskkill
            # from stop_app.bat); the FlowEngine v2 backtest needs it to map tokens.
            self._write_token_metadata()

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
            # Normalize schema fields. Captures last_traded_qty + best-5 depth so FlowEngine v2
            # (quote-based trade classification: P>=ask buy / P<=bid sell) can be backtested on
            # recorded data. Depth is flattened into bid1-5/ask1-5 price/qty/orders columns.
            records = []
            for t in ticks:
                rec = {
                    "token": str(t.get("token", "")),
                    "exchange": str(t.get("exchange", "")),
                    "ltp": float(t.get("ltp", 0.0)),
                    "last_traded_qty": float(t.get("last_traded_qty", 0.0)),
                    "volume": float(t.get("volume", 0.0)),
                    "total_buy_qty": float(t.get("total_buy_qty", 0.0)),
                    "total_sell_qty": float(t.get("total_sell_qty", 0.0)),
                    "open_interest": float(t.get("open_interest", 0.0)),
                    "open": float(t.get("open", 0.0)),
                    "high": float(t.get("high", 0.0)),
                    "low": float(t.get("low", 0.0)),
                    "close": float(t.get("close", 0.0)),
                    "exchange_timestamp": int(t.get("exchange_timestamp", 0)),
                    "received_at": str(t.get("received_at", datetime.now(timezone.utc).isoformat()))
                }
                depth = t.get("depth") or {}
                bids = depth.get("bids", [])
                asks = depth.get("asks", [])
                for lvl in range(5):
                    b = bids[lvl] if lvl < len(bids) else {}
                    a = asks[lvl] if lvl < len(asks) else {}
                    rec[f"bid{lvl+1}_price"] = float(b.get("price", 0.0))
                    rec[f"bid{lvl+1}_qty"] = float(b.get("qty", 0.0))
                    rec[f"bid{lvl+1}_orders"] = int(b.get("orders", 0))
                    rec[f"ask{lvl+1}_price"] = float(a.get("price", 0.0))
                    rec[f"ask{lvl+1}_qty"] = float(a.get("qty", 0.0))
                    rec[f"ask{lvl+1}_orders"] = int(a.get("orders", 0))
                records.append(rec)

            table = pa.Table.from_pylist(records)
            pq.write_table(table, filepath, compression="snappy")
            logger.info(f"💾 Flushed {len(ticks)} ticks to Parquet: {filepath}")

        except Exception as e:
            logger.error(f"Error writing Parquet partition: {e}")

if __name__ == "__main__":
    recorder = TickRecorder()
    asyncio.run(recorder.start())
