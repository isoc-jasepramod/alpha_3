"""
OI Squeeze EXIT-redesign experiment (advisory-only, offline).

Holds the REAL captured OI Squeeze entries fixed (same strategy + governor as the live
backtest) and re-resolves them against a range of exit brackets, to isolate whether the
losing expectancy is an EXIT problem (given the consistent ~+11% MFE / -8.5% MAE profile)
rather than an entry problem.

For each signal we record the full forward option-premium path from entry, then replay
several exit rules over that identical path:
  A) Fixed stop/target brackets (% of entry premium) — stop checked before target per tick.
  B) Trailing stop (give back X% from the running peak).
  C) Time stop (exit after N minutes).

Reports avg return/trade (expectancy) and win-rate per rule. Conservative fills:
within a tick, a stop breach counts as a stop even if a later tick would reach target.

Usage: python lab_services/backtest_oi_squeeze_exits.py [YYYY-MM-DD]
"""
import os
import sys
import glob
import json
import bisect
import asyncio
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyarrow.parquet as pq
from backend.strategies.oi_squeeze import OISqueezeSentinel
from backend.risk.risk_governor import RiskGovernor
from backend.strategies.indicators import CandleAggregator

LAKE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "lake")
SPOT_TOKENS = {"26000": "NIFTY", "99926000": "NIFTY", "99919000": "SENSEX"}
STRIKE_STEP = {"NIFTY": 50.0, "SENSEX": 100.0}


def list_days():
    return sorted(os.path.basename(d).split("=")[1]
                  for d in glob.glob(os.path.join(LAKE, "date=*")) if os.path.isdir(d))


def load_meta(day):
    p = os.path.join(LAKE, f"date={day}", "token_meta.json")
    return json.load(open(p)) if os.path.exists(p) else {}


def load_rows(day):
    files = sorted(glob.glob(os.path.join(LAKE, f"date={day}", "ticks_*.parquet")))
    cols = ["token", "ltp", "volume", "open_interest", "total_buy_qty",
            "total_sell_qty", "exchange_timestamp"]
    rows = []
    for fp in files:
        have = set(pq.read_table(fp).schema.names)
        use = [c for c in cols if c in have]
        d = pq.read_table(fp, columns=use).to_pydict()
        for i in range(len(d["token"])):
            rows.append({c: d[c][i] for c in use})
    rows.sort(key=lambda r: r["exchange_timestamp"])
    return rows


def ts_sec(ms):
    s = float(ms)
    return s / 1000.0 if s > 1e11 else s


async def capture_entries_with_paths(day):
    meta_map = load_meta(day)
    if not meta_map:
        return []
    rows = load_rows(day)
    if not rows:
        return []

    strat = OISqueezeSentinel({"start_time": "00:00:00", "end_time": "23:59:59"})
    gov = RiskGovernor()
    spot_now = {"NIFTY": 0.0, "SENSEX": 0.0}
    gov_spot_agg = {"NIFTY": CandleAggregator(60), "SENSEX": CandleAggregator(60)}
    price_tl = defaultdict(list)
    signals = []

    for r in rows:
        token = str(r.get("token", ""))
        ltp = float(r.get("ltp", 0.0) or 0.0)
        if ltp <= 0:
            continue
        t = ts_sec(r.get("exchange_timestamp", 0))
        m = meta_map.get(token)

        if token in SPOT_TOKENS or (m and m.get("is_spot")):
            inst = SPOT_TOKENS.get(token) or m.get("name", "NIFTY")
            spot_now[inst] = ltp
            await strat.on_tick({"token": token, "ltp": ltp, "volume": 0.0,
                                 "exchange_timestamp": r.get("exchange_timestamp", 0)},
                                {"is_spot": True, "name": inst})
            if inst in gov_spot_agg:
                bar = gov_spot_agg[inst].on_tick(t, ltp, volume=1.0)
                if bar:
                    gov.update_spot_bar(inst, bar["high"], bar["low"], bar["close"])
            continue

        if not m or not m.get("option_type"):
            continue
        price_tl[token].append((t, ltp))

        inst = m.get("name", "NIFTY")
        strike = float(m.get("strike", 0.0))
        step = STRIKE_STEP.get(inst, 50.0)
        sp = spot_now.get(inst, 0.0)
        offset = int(round((strike - round(sp / step) * step) / step)) if (sp > 0 and strike > 0) else None

        cand = await strat.on_tick(
            {"token": token, "ltp": ltp, "volume": float(r.get("volume", 0.0) or 0.0),
             "open_interest": float(r.get("open_interest", 0.0) or 0.0),
             "total_buy_qty": float(r.get("total_buy_qty", 0.0) or 0.0),
             "total_sell_qty": float(r.get("total_sell_qty", 0.0) or 0.0),
             "exchange_timestamp": r.get("exchange_timestamp", 0)},
            {"name": inst, "is_spot": False, "option_type": m.get("option_type"),
             "strike": strike, "lot_size": int(m.get("lot_size", 50) or 50),
             "symbol": m.get("symbol", token), "offset": offset, "expiry": m.get("expiry", "")},
        )
        if cand:
            final = gov.evaluate_signal(cand)
            if final:
                signals.append({"day": day, "token": token, "inst": inst,
                                "type": cand["details"].get("squeeze_type"),
                                "entry_ts": t, "entry": float(final["entry_price"])})

    # attach forward path (list of prices after entry) to each signal
    tl_ts = {tok: [x[0] for x in tl] for tok, tl in price_tl.items()}
    tl_px = {tok: [x[1] for x in tl] for tok, tl in price_tl.items()}
    for s in signals:
        ts_arr = tl_ts.get(s["token"], [])
        start = bisect.bisect_right(ts_arr, s["entry_ts"])
        s["path"] = list(zip(ts_arr[start:], tl_px.get(s["token"], [])[start:]))
    return signals


# ---------------- exit rules (operate on entry + forward path) ----------------
def resolve_fixed(entry, path, stop_pct, target_pct):
    stop = entry * (1 - stop_pct / 100.0)
    target = entry * (1 + target_pct / 100.0)
    for _, p in path:
        if p <= stop:
            return -stop_pct
        if p >= target:
            return target_pct
    return (path[-1][1] - entry) / entry * 100.0 if path else 0.0


def resolve_trailing(entry, path, stop_pct, give_back_pct):
    hard_stop = entry * (1 - stop_pct / 100.0)
    peak = entry
    for _, p in path:
        if p <= hard_stop:
            return (hard_stop - entry) / entry * 100.0
        peak = max(peak, p)
        trail = peak * (1 - give_back_pct / 100.0)
        if p <= trail and peak > entry:  # only trail once in profit
            return (max(trail, hard_stop) - entry) / entry * 100.0
    return (path[-1][1] - entry) / entry * 100.0 if path else 0.0


def resolve_time(entry, path, stop_pct, minutes):
    hard_stop = entry * (1 - stop_pct / 100.0)
    t0 = path[0][0] if path else 0
    last = entry
    for t, p in path:
        if p <= hard_stop:
            return (hard_stop - entry) / entry * 100.0
        last = p
        if t - t0 >= minutes * 60:
            return (p - entry) / entry * 100.0
    return (last - entry) / entry * 100.0


def stats(rets):
    if not rets:
        return "no trades"
    n = len(rets)
    wins = sum(1 for r in rets if r > 0)
    avg = sum(rets) / n
    return f"n={n:>3}  win={wins/n*100:4.0f}%  expectancy/trade={avg:+6.2f}%"


async def main():
    days = [sys.argv[1]] if len(sys.argv) > 1 else list_days()
    sigs = []
    for d in days:
        sigs += await capture_entries_with_paths(d)
    sigs = [s for s in sigs if s.get("path")]
    print(f"Captured {len(sigs)} OI Squeeze entries with forward paths.\n")

    print("=== BASELINE (live governor: ~20% stop, 2R≈+40% target) ===")
    print("  " + stats([resolve_fixed(s["entry"], s["path"], 20.0, 40.0) for s in sigs]))

    print("\n=== A) FIXED brackets (stop% / target%) ===")
    for stop_pct in (15.0, 20.0, 25.0):
        for tgt_pct in (10.0, 15.0, 20.0, 25.0, 30.0):
            label = f"  stop -{stop_pct:>4.0f}% / tgt +{tgt_pct:>4.0f}%:"
            print(label, stats([resolve_fixed(s["entry"], s["path"], stop_pct, tgt_pct) for s in sigs]))
        print()

    print("=== B) TRAILING stop (hard stop% / give-back% from peak) ===")
    for stop_pct in (20.0, 25.0):
        for gb in (6.0, 10.0, 15.0):
            label = f"  stop -{stop_pct:>4.0f}% / trail {gb:>4.0f}%:"
            print(label, stats([resolve_trailing(s["entry"], s["path"], stop_pct, gb) for s in sigs]))
        print()

    print("=== C) TIME stop (hard stop% / minutes) ===")
    for stop_pct in (20.0, 25.0):
        for mins in (3, 5, 10):
            label = f"  stop -{stop_pct:>4.0f}% / {mins:>2}min:"
            print(label, stats([resolve_time(s["entry"], s["path"], stop_pct, mins) for s in sigs]))
        print()

    print("Interpretation: if some exit rule flips expectancy clearly positive on the SAME")
    print("entries, the problem is the exit bracket, not the signal. If nothing does, the")
    print("entries lack edge. Small sample (3 days) — directional, not conclusive.")


if __name__ == "__main__":
    asyncio.run(main())
