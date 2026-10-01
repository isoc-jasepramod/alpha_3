"""
FlowEngine v2 validation harness (advisory-only, offline).

Question: does FlowEngineV2's per-option FlowIndex carry predictive edge over a
coin flip for that option's OWN forward premium move?

Method (honest, measure-before-trust):
  1. Replay a day's recorded option ticks through FlowEngineV2 (enabled), rebuilding
     the best_bid/best_ask/depth/last_traded_qty fields the live binary parser adds.
  2. At each tick where v2 produced a reading, record (flow_index, confidence) and the
     option's LTP.
  3. For each reading, look up that same option's LTP at +60s / +180s / +300s and compute
     the forward return.
  4. A "prediction" = sign(flow_index). A "hit" = forward return has the same sign.
     Compare hit-rate vs the 50% coin-flip baseline, overall and gated by confidence.
     Also report mean forward return conditioned on the predicted direction (an edge in
     hit-rate is only useful if the winning moves aren't dwarfed by the losing ones).

Usage:
  python lab_services/validate_flow_v2.py                 # default: 2026-10-01
  python lab_services/validate_flow_v2.py 2026-10-01
"""
import os
import sys
import glob
import json
import math
import bisect
import asyncio
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyarrow.parquet as pq
from backend.strategies.flow_engine_v2 import FlowEngineV2

LAKE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "lake")
HORIZONS = (60.0, 180.0, 300.0)   # forward-return horizons in seconds
CONF_GATES = (0.0, 60.0, 80.0)    # confidence thresholds to test


def load_meta(date_str):
    p = os.path.join(LAKE, f"date={date_str}", "token_meta.json")
    if not os.path.exists(p):
        return {}
    with open(p) as f:
        return json.load(f)


def load_rows(date_str):
    files = sorted(glob.glob(os.path.join(LAKE, f"date={date_str}", "ticks_*.parquet")))
    if not files:
        raise SystemExit(f"No parquet files for date={date_str}")
    depth_cols = []
    for lvl in range(1, 6):
        depth_cols += [f"bid{lvl}_price", f"bid{lvl}_qty", f"bid{lvl}_orders",
                       f"ask{lvl}_price", f"ask{lvl}_qty", f"ask{lvl}_orders"]
    base_cols = ["token", "ltp", "last_traded_qty", "volume", "open_interest",
                 "total_buy_qty", "total_sell_qty", "exchange_timestamp"]
    rows = []
    for fp in files:
        t = pq.read_table(fp)
        have = set(t.schema.names)
        cols = [c for c in base_cols + depth_cols if c in have]
        d = pq.read_table(fp, columns=cols).to_pydict()
        n = t.num_rows
        for i in range(n):
            rows.append({c: d[c][i] for c in cols})
    rows.sort(key=lambda r: r["exchange_timestamp"])
    return rows


def build_tick(r):
    """Reconstruct the live-parser tick shape (best_bid/best_ask/depth) from flat columns."""
    tick = {
        "token": str(r.get("token", "")),
        "ltp": float(r.get("ltp", 0.0) or 0.0),
        "last_traded_qty": float(r.get("last_traded_qty", 0.0) or 0.0),
        "volume": float(r.get("volume", 0.0) or 0.0),
        "open_interest": float(r.get("open_interest", 0.0) or 0.0),
        "total_buy_qty": float(r.get("total_buy_qty", 0.0) or 0.0),
        "total_sell_qty": float(r.get("total_sell_qty", 0.0) or 0.0),
        "exchange_timestamp": int(r.get("exchange_timestamp", 0) or 0),
    }
    bids, asks = [], []
    for lvl in range(1, 6):
        bp = float(r.get(f"bid{lvl}_price", 0.0) or 0.0)
        ap = float(r.get(f"ask{lvl}_price", 0.0) or 0.0)
        if bp > 0:
            bids.append({"price": bp, "qty": float(r.get(f"bid{lvl}_qty", 0.0) or 0.0),
                         "orders": int(r.get(f"bid{lvl}_orders", 0) or 0)})
        if ap > 0:
            asks.append({"price": ap, "qty": float(r.get(f"ask{lvl}_qty", 0.0) or 0.0),
                         "orders": int(r.get(f"ask{lvl}_orders", 0) or 0)})
    if bids or asks:
        tick["depth"] = {"bids": bids, "asks": asks}
        if bids:
            tick["best_bid"] = bids[0]["price"]
        if asks:
            tick["best_ask"] = asks[0]["price"]
    return tick


def ts_sec(ms):
    s = float(ms)
    return s / 1000.0 if s > 1e11 else s


async def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else "2026-10-01"
    meta_map = load_meta(date_str)
    rows = load_rows(date_str)
    print(f"[{date_str}] loaded {len(rows):,} rows; {len(meta_map)} tokens in sidecar.")

    eng = FlowEngineV2({"enabled": True, "start_time": "00:00:00", "end_time": "23:59:59"})

    # price timeline per token for forward-return lookup
    price_tl = defaultdict(list)   # token -> [(ts, ltp)]
    # recorded predictions: token -> [(ts, flow_index, confidence, ltp)]
    preds = defaultdict(list)
    skipped_meta = set()

    for r in rows:
        token = str(r.get("token", ""))
        m = meta_map.get(token)
        if not m or m.get("is_spot") or not m.get("option_type"):
            if not m:
                skipped_meta.add(token)
            # still record spot price timeline? not needed; v2 is per-option
            continue

        tick = build_tick(r)
        if tick["ltp"] <= 0:
            continue
        t = ts_sec(tick["exchange_timestamp"])
        price_tl[token].append((t, tick["ltp"]))

        await eng.on_tick(tick, {
            "name": m.get("name"), "is_spot": False,
            "option_type": m.get("option_type"), "strike": m.get("strike"),
            "lot_size": m.get("lot_size", 0),
        })
        latest = eng._latest.get(token)
        if latest and latest["timestamp"] == t:
            preds[token].append((t, latest["flow_index"], latest["confidence"], tick["ltp"]))

    # ---- forward-return evaluation ----
    # Pre-split each token timeline into parallel ts/price arrays for bisect lookup.
    tl_ts = {tok: [x[0] for x in tl] for tok, tl in price_tl.items()}
    tl_px = {tok: [x[1] for x in tl] for tok, tl in price_tl.items()}

    def fwd_price(token, t0, horizon):
        ts_arr = tl_ts.get(token)
        if not ts_arr:
            return None
        target = t0 + horizon
        idx = bisect.bisect_left(ts_arr, target)
        if idx >= len(ts_arr):
            return None
        return tl_px[token][idx]

    print(f"\nTokens with predictions: {len(preds)}; skipped (no meta): {len(skipped_meta)}")
    total_preds = sum(len(v) for v in preds.values())
    print(f"Total v2 readings: {total_preds:,}\n")

    for horizon in HORIZONS:
        print(f"===== Forward horizon: {int(horizon)}s =====")
        for gate in CONF_GATES:
            n = hits = 0
            dir_rets = []
            for token, plist in preds.items():
                for (t0, fi, conf, p0) in plist:
                    if fi == 0 or conf < gate:
                        continue
                    pf = fwd_price(token, t0, horizon)
                    if pf is None or p0 <= 0:
                        continue
                    ret = (pf - p0) / p0
                    pred_dir = 1 if fi > 0 else -1
                    n += 1
                    if (ret > 0 and pred_dir > 0) or (ret < 0 and pred_dir < 0):
                        hits += 1
                    dir_rets.append(ret * pred_dir)  # signed by prediction
                    # ret*pred_dir > 0 means prediction was right
            if n == 0:
                print(f"  conf>={int(gate):>2}:  no samples")
                continue
            hr = hits / n * 100.0
            mean_dir_ret = sum(dir_rets) / len(dir_rets) * 100.0
            print(f"  conf>={int(gate):>2}:  n={n:>7,}  hit-rate={hr:5.1f}%  "
                  f"(baseline 50.0%)  mean dir-return={mean_dir_ret:+.3f}%")
        print()

    print("Interpretation: hit-rate meaningfully above 50% AND positive mean dir-return")
    print("at higher confidence gates = real edge. Flat ~50% / negative = no edge (keep disabled).")
    print("NOTE: single day only. Repeat across multiple recorded days before trusting.")


if __name__ == "__main__":
    asyncio.run(main())
