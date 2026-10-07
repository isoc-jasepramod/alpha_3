"""
LAB SERVICES — INSTITUTIONAL FLOW ENGINE (AIFI) VALIDATION HARNESS
==================================================================
Evaluates whether the Aggregate Institutional Flow Index (AIFI) provides
a genuine predictive edge for the underlying Spot Index forward movement.

Evaluates against:
- 3-Minute Forward Spot Return
- 5-Minute Forward Spot Return
- 15-Minute Forward Spot Return

Baseline comparison: 50.0% random-walk coin flip.
"""

import os
import sys
import glob
import json
import bisect
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from typing import Dict, Any, Optional, List, Tuple
import pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.strategies.institutional_flow_engine import InstitutionalFlowEngine

LAKE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "lake")
HORIZONS = (180.0, 300.0, 900.0)  # 3m, 5m, 15m in seconds
IST = timezone(timedelta(hours=5, minutes=30))


def load_meta(date_str: str) -> Dict[str, Any]:
    p = os.path.join(LAKE, f"date={date_str}", "token_meta.json")
    if not os.path.exists(p):
        return {}
    with open(p, "r") as f:
        return json.load(f)


def validate_flow_day(date_str: str = "2026-10-07", instrument: str = "NIFTY"):
    lake_dir = os.path.join(LAKE, f"date={date_str}")
    files = sorted(glob.glob(os.path.join(lake_dir, "*.parquet")))
    if not files:
        print(f"No parquet files found for {date_str}")
        return

    meta_map = load_meta(date_str)
    print(f"\n{'='*85}")
    print(f"AIFI FLOW VALIDATION FOR {instrument} ON {date_str} ({len(files)} files)")
    print(f"{'='*85}")

    engine = InstitutionalFlowEngine(config={"window_sec": 60.0})

    # Track spot price timeline for forward return evaluation
    # [(ts_sec, spot_price)]
    spot_tl: List[Tuple[float, float]] = []

    # Track flow predictions: [(ts_sec, aifi, direction, confidence, spot_at_time)]
    predictions: List[Tuple[float, float, str, int, float]] = []

    spot_token = "26000" if instrument == "NIFTY" else "99919000"
    alt_token = "99926000" if instrument == "NIFTY" else "99919000"

    current_spot = 0.0
    last_pred_ts = 0.0

    cols = ["token", "ltp", "volume", "open_interest", "exchange_timestamp",
            "bid1_qty", "ask1_qty", "bid2_qty", "ask2_qty"]

    for fp in files:
        tbl = pq.read_table(fp)
        have_cols = [c for c in cols if c in tbl.schema.names]
        d = tbl.select(have_cols).to_pydict()

        tokens = d["token"]
        ltps = d["ltp"]
        tss = d["exchange_timestamp"]
        vols = d.get("volume", [0] * len(tokens))
        ois = d.get("open_interest", [0] * len(tokens))
        b1 = d.get("bid1_qty", [0] * len(tokens))
        a1 = d.get("ask1_qty", [0] * len(tokens))

        for i in range(len(tokens)):
            tok = str(tokens[i])
            ltp = float(ltps[i] or 0.0)
            if ltp <= 0:
                continue

            raw_ts = float(tss[i] or 0.0)
            ts = raw_ts if raw_ts < 1e11 else raw_ts / 1000.0

            # Record Spot price timeline
            if tok in (spot_token, alt_token):
                current_spot = ltp
                spot_tl.append((ts, ltp))
                continue

            # Process Option ticks
            meta = meta_map.get(tok)
            if not meta or meta.get("name") != instrument or meta.get("is_spot"):
                continue

            tick = {
                "token": tok,
                "ltp": ltp,
                "volume": float(vols[i] or 0.0),
                "open_interest": float(ois[i] or 0.0),
                "exchange_timestamp": ts,
                "bid1_qty": float(b1[i] or 0.0),
                "ask1_qty": float(a1[i] or 0.0),
            }

            st = engine.on_tick(tick, meta)
            if st and current_spot > 0:
                # Sample prediction every 30 seconds
                if ts - last_pred_ts >= 30.0:
                    last_pred_ts = ts
                    aifi = st["aifi"]
                    direction = st["direction"]
                    conf = st["confidence"]
                    predictions.append((ts, aifi, direction, conf, current_spot))

    if not spot_tl or not predictions:
        print("Insufficient spot or flow data.")
        return

    # Sort spot timeline
    spot_tl.sort(key=lambda x: x[0])
    spot_ts = [x[0] for x in spot_tl]
    spot_px = [x[1] for x in spot_tl]

    def get_forward_spot(t0: float, horizon_sec: float) -> Optional[float]:
        target = t0 + horizon_sec
        idx = bisect.bisect_left(spot_ts, target)
        if idx < len(spot_ts):
            return spot_px[idx]
        return None

    print(f"Recorded {len(predictions):,} flow predictions across session.")
    print(f"Spot prices tracked: {len(spot_tl):,} ticks.\n")

    for horizon in HORIZONS:
        h_min = int(horizon / 60)
        print(f"--- Horizon: {h_min} Minutes ({int(horizon)}s) ---")
        for min_conf in (30, 50, 70):
            n = 0
            hits = 0
            ret_pts = []

            for (t0, aifi, direction, conf, p0) in predictions:
                if direction == "NEUTRAL" or conf < min_conf:
                    continue

                p_fwd = get_forward_spot(t0, horizon)
                if p_fwd is None:
                    continue

                diff = p_fwd - p0
                n += 1

                if direction == "BULLISH":
                    if diff > 0:
                        hits += 1
                    ret_pts.append(diff)
                elif direction == "BEARISH":
                    if diff < 0:
                        hits += 1
                    ret_pts.append(-diff)  # profit from short

            if n > 0:
                hit_rate = (hits / n) * 100.0
                mean_gain = sum(ret_pts) / n
                print(f"  Confidence >= {min_conf}%:  Predictions={n:>4}  |  Hit Rate = {hit_rate:>5.1f}%  |  Mean Spot Edge = {mean_gain:>+5.2f} pts")
            else:
                print(f"  Confidence >= {min_conf}%:  No qualifying samples")
        print()


if __name__ == "__main__":
    day = sys.argv[1] if len(sys.argv) > 1 else "2026-10-07"
    validate_flow_day(day, "NIFTY")
    validate_flow_day(day, "SENSEX")
