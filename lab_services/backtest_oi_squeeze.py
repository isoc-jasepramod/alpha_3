"""
OI Squeeze outcome backtest (advisory-only, offline).

Replays recorded ticks through the REAL OISqueezeSentinel + REAL RiskGovernor (exactly as
the live system wires them), captures every emitted signal, and resolves each against the
option's OWN forward price path to STOP / TARGET / EOD.

Honest design:
  - Uses the actual strategy & governor classes (no reimplementation) so entries, stops,
    and 2R targets are authentic.
  - Resolves on the option premium path tick-by-tick: whichever of stop/target is hit first.
    If neither by end-of-data, exits at last price (EOD).
  - Reports results split by squeeze_type (STANDARD vs EARLY_IGNITION) and instrument, plus
    raw forward-return and max-favorable/adverse excursion so conclusions don't depend only
    on the chosen bracket.

Usage:
  python lab_services/backtest_oi_squeeze.py                       # all recorded days
  python lab_services/backtest_oi_squeeze.py 2026-10-01            # one day
"""
import os
import sys
import glob
import json
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
            r = {c: d[c][i] for c in use}
            rows.append(r)
    rows.sort(key=lambda r: r["exchange_timestamp"])
    return rows


def ts_sec(ms):
    s = float(ms)
    return s / 1000.0 if s > 1e11 else s


async def run_day(day):
    meta_map = load_meta(day)
    if not meta_map:
        return []
    rows = load_rows(day)
    if not rows:
        return []

    strat = OISqueezeSentinel({"start_time": "00:00:00", "end_time": "23:59:59"})
    gov = RiskGovernor()

    spot_now = {"NIFTY": 0.0, "SENSEX": 0.0}
    # 1m spot aggregators to feed the governor's ATR14 with real bars (realistic stop distance)
    gov_spot_agg = {"NIFTY": CandleAggregator(timeframe_seconds=60),
                    "SENSEX": CandleAggregator(timeframe_seconds=60)}
    # option price timeline for forward resolution: token -> [(ts, ltp)]
    price_tl = defaultdict(list)
    signals = []

    for r in rows:
        token = str(r.get("token", ""))
        ltp = float(r.get("ltp", 0.0) or 0.0)
        if ltp <= 0:
            continue
        t = ts_sec(r.get("exchange_timestamp", 0))
        m = meta_map.get(token)

        # Spot handling (also feed governor ATR via 1m bars from strat's aggregator is internal;
        # we feed governor spot bars separately below)
        if token in SPOT_TOKENS or (m and m.get("is_spot")):
            inst = SPOT_TOKENS.get(token) or m.get("name", "NIFTY")
            spot_now[inst] = ltp
            await strat.on_tick(
                {"token": token, "ltp": ltp, "volume": 0.0, "exchange_timestamp": r.get("exchange_timestamp", 0)},
                {"is_spot": True, "name": inst},
            )
            # feed governor ATR14 with real 1m spot bars for a realistic stop distance
            if inst in gov_spot_agg:
                bar = gov_spot_agg[inst].on_tick(t, ltp, volume=1.0)
                if bar:
                    gov.update_spot_bar(inst, bar["high"], bar["low"], bar["close"])
            continue

        if not m or not m.get("option_type"):
            continue

        price_tl[token].append((t, ltp))

        # compute ATM offset from current spot
        inst = m.get("name", "NIFTY")
        strike = float(m.get("strike", 0.0))
        step = STRIKE_STEP.get(inst, 50.0)
        sp = spot_now.get(inst, 0.0)
        offset = None
        if sp > 0 and strike > 0:
            atm = round(sp / step) * step
            offset = int(round((strike - atm) / step))

        tick = {
            "token": token, "ltp": ltp,
            "volume": float(r.get("volume", 0.0) or 0.0),
            "open_interest": float(r.get("open_interest", 0.0) or 0.0),
            "total_buy_qty": float(r.get("total_buy_qty", 0.0) or 0.0),
            "total_sell_qty": float(r.get("total_sell_qty", 0.0) or 0.0),
            "exchange_timestamp": r.get("exchange_timestamp", 0),
        }
        meta = {
            "name": inst, "is_spot": False, "option_type": m.get("option_type"),
            "strike": strike, "lot_size": int(m.get("lot_size", 50) or 50),
            "symbol": m.get("symbol", token), "offset": offset, "expiry": m.get("expiry", ""),
        }
        cand = await strat.on_tick(tick, meta)
        if cand:
            # feed governor a rough ATR seed from recent spot if empty, then evaluate
            final = gov.evaluate_signal(cand)
            if final:
                signals.append({
                    "day": day, "token": token, "inst": inst,
                    "type": cand["details"].get("squeeze_type"),
                    "direction": cand["direction"], "entry_ts": t,
                    "entry": float(final["entry_price"]),
                    "stop": float(final["stop_loss"]), "target": float(final["target"]),
                })

    # ---- resolve each signal on its option's forward path ----
    import bisect
    tl_ts = {tok: [x[0] for x in tl] for tok, tl in price_tl.items()}
    tl_px = {tok: [x[1] for x in tl] for tok, tl in price_tl.items()}

    for s in signals:
        ts_arr = tl_ts.get(s["token"], [])
        px_arr = tl_px.get(s["token"], [])
        start = bisect.bisect_right(ts_arr, s["entry_ts"])
        outcome = "EOD"; exit_px = s["entry"]; mfe = s["entry"]; mae = s["entry"]
        for i in range(start, len(px_arr)):
            p = px_arr[i]
            mfe = max(mfe, p); mae = min(mae, p)
            if p <= s["stop"]:
                outcome = "STOP"; exit_px = s["stop"]; break
            if p >= s["target"]:
                outcome = "TARGET"; exit_px = s["target"]; break
        else:
            if px_arr and start < len(px_arr):
                exit_px = px_arr[-1]
        s["outcome"] = outcome
        s["exit"] = exit_px
        s["ret_pct"] = (exit_px - s["entry"]) / s["entry"] * 100.0
        s["mfe_pct"] = (mfe - s["entry"]) / s["entry"] * 100.0
        s["mae_pct"] = (mae - s["entry"]) / s["entry"] * 100.0
    return signals


def summarize(sigs, label):
    if not sigs:
        print(f"  {label}: no signals"); return
    n = len(sigs)
    wins = sum(1 for s in sigs if s["outcome"] == "TARGET")
    stops = sum(1 for s in sigs if s["outcome"] == "STOP")
    eod = sum(1 for s in sigs if s["outcome"] == "EOD")
    avg_ret = sum(s["ret_pct"] for s in sigs) / n
    avg_mfe = sum(s["mfe_pct"] for s in sigs) / n
    avg_mae = sum(s["mae_pct"] for s in sigs) / n
    print(f"  {label}: n={n}  TARGET={wins} ({wins/n*100:.0f}%)  STOP={stops} ({stops/n*100:.0f}%)  EOD={eod}")
    print(f"       avg premium return/trade: {avg_ret:+.1f}%   avg MFE {avg_mfe:+.1f}%  avg MAE {avg_mae:+.1f}%")


async def main():
    days = [sys.argv[1]] if len(sys.argv) > 1 else list_days()
    allsig = []
    for day in days:
        sigs = await run_day(day)
        if sigs:
            print(f"\n===== {day} =====  ({len(sigs)} signals)")
            summarize([s for s in sigs if s["type"] == "STANDARD_SQUEEZE"], "STANDARD")
            summarize([s for s in sigs if s["type"] == "EARLY_IGNITION"], "EARLY_IGNITION")
            allsig += sigs

    print("\n================ AGGREGATE (all days) ================")
    summarize(allsig, "ALL")
    summarize([s for s in allsig if s["type"] == "STANDARD_SQUEEZE"], "STANDARD")
    summarize([s for s in allsig if s["type"] == "EARLY_IGNITION"], "EARLY_IGNITION")
    for inst in ("NIFTY", "SENSEX"):
        summarize([s for s in allsig if s["inst"] == inst], inst)
    print("\nNote: resolves on option premium to the governor's 2R target / ~20%-floor stop.")
    print("MFE/MAE show how far trades ran in favor/against regardless of bracket.")


if __name__ == "__main__":
    asyncio.run(main())
