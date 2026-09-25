import sys
import os
import asyncio
import time
from datetime import datetime, timezone, timedelta

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import httpx
from loguru import logger
from backend.core.redis_bus import RedisBus
from backend.core.instrument_manager import InstrumentManager

IST = timezone(timedelta(hours=5, minutes=30))

async def run_e2e_simulation():
    logger.info("🧪 Starting Alpha 2.0 Comprehensive End-to-End Simulation...")
    redis_bus = RedisBus.from_config()
    await redis_bus.connect()

    mgr = InstrumentManager()
    await mgr.sync_master()
    wings = mgr.get_atm_and_wings("NIFTY", 25000.0)
    ce_meta = wings["by_strike"][25000.0]["CE"]
    token_ce = str(ce_meta["token"])
    logger.info(f"Target ATM CE: {ce_meta['symbol']} (Token: {token_ce})")

    # Fixed IST morning timestamp (09:25:00 AM IST)
    today = datetime.now(IST).date()
    # Monotonically advancing time based on epoch to avoid cooldown clashes across runs
    time_offset = int(time.time()) % 10000
    base_dt = datetime(today.year, today.month, today.day, 9, 30, 0, tzinfo=IST) + timedelta(seconds=time_offset)
    base_ts = int(base_dt.timestamp())

    # 1. Warm up Spot ticks: Establish EMA20 above 24900
    logger.info("Step 1: Warming up Spot Index ticks (NIFTY 25000)...")
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
    await asyncio.sleep(0.5)

    # 2. Establish baseline for Option contract
    logger.info("Step 2: Establishing Option baseline (P=100, OI=1,000,000, Vol=5000)...")
    base_opt_tick = {
        "token": token_ce,
        "exchange": "nfo_fo",
        "ltp": 100.0,
        "volume": 5000.0,
        "open_interest": 1000000.0,
        "exchange_timestamp": base_ts + 25,
        "received_at": datetime.now(timezone.utc).isoformat()
    }
    await redis_bus.publish_tick(base_opt_tick)
    await asyncio.sleep(0.2)

    # 3. Trigger OI Squeeze:
    # Delta OI drops by -10.0% (1,000,000 -> 900,000)
    # Delta Price spikes +6.0% (100 -> 106)
    # Volume surges
    logger.info("Step 3: Triggering OI Squeeze Capitulation (dOI: -10%, dPrice: +6%)...")
    trigger_opt_tick = {
        "token": token_ce,
        "exchange": "nfo_fo",
        "ltp": 106.0,
        "volume": 25000.0,
        "open_interest": 900000.0, # -10.0% drop
        "exchange_timestamp": base_ts + 60,
        "received_at": datetime.now(timezone.utc).isoformat()
    }
    await redis_bus.publish_tick(trigger_opt_tick)
    await asyncio.sleep(1.0)

    # 4. Verify Active Signal generated
    async with httpx.AsyncClient() as client:
        res = await client.get("http://127.0.0.1:8000/api/signals/active")
        active_sigs = res.json()
        logger.info(f"Active Signals in Terminal: {len(active_sigs)}")
        assert len(active_sigs) >= 1, "Expected at least 1 active signal!"
        sig = active_sigs[0]
        sig_id = sig["signal_id"]
        entry_p = sig["entry_price"]
        sl = sig["stop_loss"]
        tgt = sig["target"]
        logger.success(f"Signal Generated: {sig_id} | Entry: ₹{entry_p} | SL: ₹{sl} | TGT: ₹{tgt}")

        # 5. Test Runaway Guardrail:
        # Price surges to Entry + 4.0% within 30 seconds -> Lock out as INVALID_CHASE_PREVENTED
        logger.info("Step 5: Simulating runaway price spike > +3.0% within first 30s...")
        runaway_tick = {
            "token": token_ce,
            "exchange": "nfo_fo",
            "ltp": round(entry_p * 1.04, 2), # +4.0% surge
            "volume": 35000.0,
            "open_interest": 890000.0,
            "exchange_timestamp": base_ts + 70,
            "received_at": datetime.now(timezone.utc).isoformat()
        }
        await redis_bus.publish_tick(runaway_tick)
        await asyncio.sleep(1.0)

        # Check DB history to verify resolution
        h_res = await client.get("http://127.0.0.1:8000/api/signals/history?limit=5")
        history = h_res.json()
        resolved_sig = next((s for s in history if s["signal_id"] == sig_id), None)
        assert resolved_sig is not None, "Resolved signal not found in DB!"
        logger.success(f"Guardrail State: {resolved_sig['status']} (Expected: INVALID_CHASE_PREVENTED)")
        assert resolved_sig["status"] == "INVALID_CHASE_PREVENTED"

        # 6. Test Target Hit Scenario:
        # Advance time past cooldown (base_ts + 300)
        logger.info("Step 6: Testing Target Hit Resolution (Trade 2)...")
        ts_trade2 = base_ts + 300
        # Base tick
        await redis_bus.publish_tick({
            "token": token_ce,
            "exchange": "nfo_fo",
            "ltp": 100.0,
            "volume": 5000.0,
            "open_interest": 1000000.0,
            "exchange_timestamp": ts_trade2,
            "received_at": datetime.now(timezone.utc).isoformat()
        })
        await asyncio.sleep(0.1)
        # Squeeze tick
        await redis_bus.publish_tick({
            "token": token_ce,
            "exchange": "nfo_fo",
            "ltp": 106.0,
            "volume": 30000.0,
            "open_interest": 900000.0,
            "exchange_timestamp": ts_trade2 + 60,
            "received_at": datetime.now(timezone.utc).isoformat()
        })
        await asyncio.sleep(1.0)

        res2 = await client.get("http://127.0.0.1:8000/api/signals/active")
        active2 = res2.json()
        assert len(active2) >= 1
        sig2 = active2[0]
        sig2_id = sig2["signal_id"]
        tgt2 = sig2["target"]
        logger.info(f"Trade 2 Generated: {sig2_id} | Target: ₹{tgt2}")

        # Simulate time passing past 30s runaway window
        from backend.api.routes import app_state
        if app_state.signal_tracker and sig2_id in app_state.signal_tracker.active_signals:
            app_state.signal_tracker.active_signals[sig2_id]["registered_ts"] -= 35

        # Send Target Hit tick
        logger.info(f"Simulating tick at Target price ₹{tgt2}...")
        await redis_bus.publish_tick({
            "token": token_ce,
            "exchange": "nfo_fo",
            "ltp": tgt2 + 0.5,
            "volume": 40000.0,
            "open_interest": 890000.0,
            "exchange_timestamp": ts_trade2 + 100,
            "received_at": datetime.now(timezone.utc).isoformat()
        })
        await asyncio.sleep(1.0)

        h_res2 = await client.get("http://127.0.0.1:8000/api/signals/history?limit=5")
        sig2_db = next((s for s in h_res2.json() if s["signal_id"] == sig2_id), None)
        assert sig2_db is not None
        logger.success(f"Trade 2 State: {sig2_db['status']} | Theoretical PnL: +₹{sig2_db['theoretical_pnl']}")
        assert sig2_db["status"] == "TARGET_HIT"
        assert sig2_db["theoretical_pnl"] > 0

        # 7. Test Stop Hit Scenario:
        logger.info("Step 7: Testing Stop Hit Resolution (Trade 3)...")
        ts_trade3 = ts_trade2 + 300
        # Base tick
        await redis_bus.publish_tick({
            "token": token_ce,
            "exchange": "nfo_fo",
            "ltp": 100.0,
            "volume": 5000.0,
            "open_interest": 1000000.0,
            "exchange_timestamp": ts_trade3,
            "received_at": datetime.now(timezone.utc).isoformat()
        })
        await asyncio.sleep(0.1)
        # Squeeze tick
        await redis_bus.publish_tick({
            "token": token_ce,
            "exchange": "nfo_fo",
            "ltp": 106.0,
            "volume": 30000.0,
            "open_interest": 900000.0,
            "exchange_timestamp": ts_trade3 + 60,
            "received_at": datetime.now(timezone.utc).isoformat()
        })
        await asyncio.sleep(1.0)

        res3 = await client.get("http://127.0.0.1:8000/api/signals/active")
        active3 = res3.json()
        assert len(active3) >= 1
        sig3 = active3[0]
        sig3_id = sig3["signal_id"]
        sl3 = sig3["stop_loss"]
        logger.info(f"Trade 3 Generated: {sig3_id} | Stop Loss: ₹{sl3}")

        # Simulate time passing past 30s
        if app_state.signal_tracker and sig3_id in app_state.signal_tracker.active_signals:
            app_state.signal_tracker.active_signals[sig3_id]["registered_ts"] -= 35

        # Send Stop Hit tick
        logger.info(f"Simulating tick at Stop price ₹{sl3}...")
        await redis_bus.publish_tick({
            "token": token_ce,
            "exchange": "nfo_fo",
            "ltp": sl3 - 0.5,
            "volume": 42000.0,
            "open_interest": 910000.0,
            "exchange_timestamp": ts_trade3 + 100,
            "received_at": datetime.now(timezone.utc).isoformat()
        })
        await asyncio.sleep(1.0)

        h_res3 = await client.get("http://127.0.0.1:8000/api/signals/history?limit=5")
        sig3_db = next((s for s in h_res3.json() if s["signal_id"] == sig3_id), None)
        assert sig3_db is not None
        logger.success(f"Trade 3 State: {sig3_db['status']} | Theoretical PnL: ₹{sig3_db['theoretical_pnl']}")
        assert sig3_db["status"] == "STOP_HIT"
        assert sig3_db["theoretical_pnl"] < 0

    await redis_bus.close()
    logger.info("🎉 All E2E Terminal States (Chase Prevented, Target Hit, Stop Hit) Verified Successfully!")

if __name__ == "__main__":
    asyncio.run(run_e2e_simulation())
