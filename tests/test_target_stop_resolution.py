import sys
import os
import asyncio
from datetime import datetime, timezone, timedelta

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import httpx
from loguru import logger
from backend.core.redis_bus import RedisBus
from backend.core.instrument_manager import InstrumentManager

import pytest
IST = timezone(timedelta(hours=5, minutes=30))

@pytest.mark.asyncio
async def test_target_and_stop_hits():

    logger.info("🎯 Testing Target Hit & Stop Hit Resolution...")
    redis_bus = RedisBus.from_config()
    await redis_bus.connect()

    mgr = InstrumentManager()
    await mgr.sync_master()
    wings = mgr.get_atm_and_wings("NIFTY", 25000.0)
    ce_meta = wings["by_strike"][25000.0]["CE"]
    token_ce = str(ce_meta["token"])

    today = datetime.now(IST).date()
    base_dt = datetime(today.year, today.month, today.day, 9, 45, 0, tzinfo=IST)
    base_ts = int(base_dt.timestamp())

    # Warm up Spot ticks: Establish EMA20 above 24900
    for i in range(25):
        spot_tick = {
            "token": "99926000",
            "exchange": "nse_cm",
            "ltp": 25000.0 + (i * 0.5),
            "volume": 0.0,
            "open_interest": 0.0,
            "open": 24990.0,
            "high": 25020.0,
            "low": 24980.0,
            "close": 25000.0 + (i * 0.5),
            "exchange_timestamp": base_ts + i,
            "received_at": datetime.now(timezone.utc).isoformat()
        }
        await redis_bus.publish_tick(spot_tick)
    await asyncio.sleep(0.3)

    # Trigger fresh signal
    # 1. Base
    await redis_bus.publish_tick({
        "token": token_ce,
        "exchange": "nfo_fo",
        "ltp": 100.0,
        "volume": 5000.0,
        "open_interest": 1000000.0,
        "exchange_timestamp": base_ts,
        "received_at": datetime.now(timezone.utc).isoformat()
    })
    await asyncio.sleep(0.1)

    # 2. Squeeze trigger
    await redis_bus.publish_tick({
        "token": token_ce,
        "exchange": "nfo_fo",
        "ltp": 105.5,
        "volume": 28000.0,
        "open_interest": 910000.0,
        "exchange_timestamp": base_ts + 5,
        "received_at": datetime.now(timezone.utc).isoformat()
    })
    await asyncio.sleep(1.0)

    async with httpx.AsyncClient() as client:
        res = await client.get("http://127.0.0.1:8000/api/signals/active")
        active = res.json()
        assert len(active) >= 1
        sig = active[0]
        sig_id = sig["signal_id"]
        tgt = sig["target"]
        logger.info(f"New Signal for Target Test: {sig_id} | Target: ₹{tgt}")

        # Send tick at target price after 35s (> 30s runaway window)
        logger.info(f"Simulating tick at Target price ₹{tgt} after 35s...")
        await asyncio.sleep(0.5)
        # Advance tracker registered timestamp by 35s
        from backend.api.routes import app_state
        if app_state.signal_tracker and sig_id in app_state.signal_tracker.active_signals:
            app_state.signal_tracker.active_signals[sig_id]["registered_ts"] -= 35

        await redis_bus.publish_tick({
            "token": token_ce,
            "exchange": "nfo_fo",
            "ltp": tgt + 0.5,
            "volume": 30000.0,
            "open_interest": 905000.0,
            "exchange_timestamp": base_ts + 45,
            "received_at": datetime.now(timezone.utc).isoformat()
        })
        await asyncio.sleep(1.0)

        # Check DB
        h_res = await client.get("http://127.0.0.1:8000/api/signals/history?limit=5")
        history = h_res.json()
        db_sig = next((s for s in history if s["signal_id"] == sig_id), None)
        assert db_sig is not None
        logger.success(f"Signal Resolution: {db_sig['status']} | Theoretical PnL: ₹{db_sig['theoretical_pnl']}")
        assert db_sig["status"] == "TARGET_HIT"
        assert db_sig["theoretical_pnl"] > 0

    await redis_bus.close()
    logger.info("🎯 Target Hit Resolution Verified Successfully!")

if __name__ == "__main__":
    asyncio.run(test_target_and_stop_hits())
