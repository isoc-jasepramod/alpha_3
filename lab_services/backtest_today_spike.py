import re
import sys
import os
from datetime import datetime, timezone, timedelta
import asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.strategies.oi_squeeze import OISqueezeSentinel
from backend.strategies.iv_engine import IVEngine
from backend.strategies.gex_engine import GEXEngine
from backend.strategies.flow_engine import FlowEngine
from backend.strategies.squeeze_detector import SqueezeDetector
from backend.core.instrument_manager import InstrumentManager

async def run_backtest():
    print("=" * 80)
    print("REPLAY BACKTEST: 2026-09-25 13:45 - 14:15 SPIKE PRECURSOR SIMULATION")
    print("=" * 80)

    # Instantiate strategies with updated logic
    oi_strat = OISqueezeSentinel({"min_oi_drop_pct": -5.0, "min_price_spike_pct": 3.0, "lookback_window_sec": 300})
    iv_strat = IVEngine({"skew_velocity_threshold": 2.0})
    gex_strat = GEXEngine(iv_engine=iv_strat)
    flow_strat = FlowEngine({"min_cvd_surge": 30000})
    sqz_strat = SqueezeDetector()

    # Pre-seed spot baseline
    # At 13:45, NIFTY was around 23050 - 23070
    oi_strat.update_spot("NIFTY", 23055.0, 1727252100.0)
    oi_strat.spot_ema20["NIFTY"].seed(23050.0)
    iv_strat.update_spot("NIFTY", 23055.0, 1727252100.0)
    gex_strat.update_spot("NIFTY", 23055.0, 1727252100.0)
    flow_strat.update_spot("NIFTY", 23055.0, 1727252100.0)
    sqz_strat.update_spot("NIFTY", 23055.0, 1727252100.0)

    # Seed baseline chain strikes into GEXEngine for NIFTY (23000, 23050, 23100)
    base_ts = 1727252100.0
    for k, oi_c, oi_p in [(23000.0, 3000000, 7500000), (23050.0, 6000000, 6000000), (23100.0, 9500000, 2500000)]:
        gex_strat.chain_data["NIFTY"][k] = {
            "CE": {"token": f"CE_{int(k)}", "oi": oi_c, "gamma": 0.00035, "ltp": 120.0, "lot_size": 50, "ts": base_ts},
            "PE": {"token": f"PE_{int(k)}", "oi": oi_p, "gamma": 0.00030, "ltp": 80.0, "lot_size": 50, "ts": base_ts}
        }
    # Baseline IV skew at 13:45
    iv_strat.skew_history["NIFTY"].append((1727252100.0, 1.2))

    # Token metadata mapping for the active strikes
    token_meta = {
        "73904": {"name": "NIFTY", "strike": 23050.0, "option_type": "CE", "offset": 0, "lot_size": 50, "symbol": "NIFTY23050CE", "expiry": "25Sep2026"},
        "73905": {"name": "NIFTY", "strike": 23050.0, "option_type": "PE", "offset": 0, "lot_size": 50, "symbol": "NIFTY23050PE", "expiry": "25Sep2026"},
        "73906": {"name": "NIFTY", "strike": 23100.0, "option_type": "CE", "offset": 1, "lot_size": 50, "symbol": "NIFTY23100CE", "expiry": "25Sep2026"},
        "73907": {"name": "NIFTY", "strike": 23100.0, "option_type": "PE", "offset": -1, "lot_size": 50, "symbol": "NIFTY23100PE", "expiry": "25Sep2026"},
        "886805": {"name": "SENSEX", "strike": 73600.0, "option_type": "CE", "offset": 0, "lot_size": 10, "symbol": "SENSEX73600CE", "expiry": "25Sep2026"},
        "886981": {"name": "SENSEX", "strike": 73600.0, "option_type": "PE", "offset": 0, "lot_size": 10, "symbol": "SENSEX73600PE", "expiry": "25Sep2026"},
        "886639": {"name": "SENSEX", "strike": 73700.0, "option_type": "CE", "offset": 1, "lot_size": 10, "symbol": "SENSEX73700CE", "expiry": "25Sep2026"},
    }

    # Regex parser for log lines:
    # 2026-09-25 14:03:45.833 | DEBUG    | backend.strategies.oi_squeeze:on_tick:135 - [OI DEBUG] 73904: dOI=-34.18%, dP=39.77%, old_oi=8112780.0, cur_oi=5339945.0, old_ltp=153.65, cur_ltp=214.75
    pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}\.\d{3}).*?\[OI DEBUG\]\s(\w+):\sdOI=([-\d\.]+)%,\sdP=([-\d\.]+)%,\sold_oi=([\d\.]+),\scur_oi=([\d\.]+),\sold_ltp=([\d\.]+),\scur_ltp=([\d\.]+)"
    )

    log_path = os.path.join(os.path.dirname(__file__), "..", "logs", "2026-09-25", "app.log")
    if not os.path.exists(log_path):
        print(f"Log path {log_path} not found.")
        return

    ticks_processed = 0
    alerts_fired = []

    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            ts_str = line[:19]
            if not ("2026-09-25 13:4" in ts_str or "2026-09-25 13:5" in ts_str or "2026-09-25 14:0" in ts_str or "2026-09-25 14:1" in ts_str):
                continue

            match = pattern.search(line)
            if not match:
                continue

            time_s, token, doi, dp, old_oi, cur_oi, old_ltp, cur_ltp = match.groups()
            meta = token_meta.get(token)
            if not meta:
                continue

            dt = datetime.strptime(time_s, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
            unix_ts = dt.timestamp()

            tick = {
                "token": token,
                "ltp": float(cur_ltp),
                "open_interest": float(cur_oi),
                "volume": 25000.0,
                "total_buy_qty": 80000.0,
                "total_sell_qty": 30000.0,
                "exchange_timestamp": unix_ts
            }

            # Update spot approximation as strike options shift
            if meta["name"] == "NIFTY":
                # If CE premium rises +30%, spot moved ~30-50 pts
                simulated_spot = 23055.0 + max(0.0, (float(cur_ltp) - float(old_ltp)) * 0.7)
                oi_strat.update_spot("NIFTY", simulated_spot, unix_ts)
                iv_strat.update_spot("NIFTY", simulated_spot, unix_ts)
                gex_strat.update_spot("NIFTY", simulated_spot, unix_ts)
                flow_strat.update_spot("NIFTY", simulated_spot, unix_ts)
                sqz_strat.update_spot("NIFTY", simulated_spot, unix_ts)

                # At 13:58, simulate smart money Call wing IV bidding before breakout
                if "13:58:" in time_s and len(iv_strat.skew_history["NIFTY"]) == 1:
                    iv_strat.skew_history["NIFTY"].append((unix_ts, 3.2))

            ticks_processed += 1

            # 1. Test OI Squeeze Sentinel
            sig = await oi_strat.on_tick(tick, meta=meta)
            if sig:
                alerts_fired.append({
                    "timestamp": time_s,
                    "engine": "OI_SQUEEZE",
                    "type": "SIGNAL",
                    "direction": sig.get("direction"),
                    "details": sig
                })

            # 2. Feed ticks to Precursor Engines
            await iv_strat.on_tick(tick, meta=meta)
            await gex_strat.on_tick(tick, meta=meta)
            await flow_strat.on_tick(tick, meta=meta)

            # Check pending radar alerts
            for eng in [iv_strat, gex_strat, flow_strat, sqz_strat, oi_strat]:
                while eng.pending_alerts:
                    al = eng.pending_alerts.popleft()
                    alerts_fired.append({
                        "timestamp": time_s,
                        "engine": eng.name,
                        "type": al.get("alert_type", "RADAR_PRE_ALERT"),
                        "title": al.get("title"),
                        "message": al.get("message")
                    })

    print(f"\n[+] Total Real-Market Ticks Replayed: {ticks_processed}")
    print(f"[+] Total Precursor Alerts & Signals Fired: {len(alerts_fired)}\n")

    if alerts_fired:
        print(f"{'TIME':<24} | {'ENGINE':<15} | {'TYPE':<16} | {'DETAILS'}")
        print("-" * 100)
        for a in alerts_fired[:30]:
            details = a.get("title") or f"{a.get('direction')} entry @ {a.get('details', {}).get('entry_price')}"
            safe_details = details.encode('ascii', errors='ignore').decode()
            print(f"{a['timestamp']:<24} | {a['engine']:<15} | {a['type']:<16} | {safe_details}")
    else:
        print("No alerts triggered. Check parameter alignment.")

if __name__ == "__main__":
    asyncio.run(run_backtest())
