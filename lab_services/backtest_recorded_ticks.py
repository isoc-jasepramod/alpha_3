"""
PROJECT ALPHA 3.0 — RECORDED-TICK BACKTEST (real OI, real premiums)

Unlike run_all_strategies_backtest.py (a candle/volume PROXY on yfinance data with a fixed
DELTA_ATM), this replays the ACTUAL recorded option ticks — including real open_interest,
volume, and buy/sell qty — through the real strategy classes. This is the first time
OI_SQUEEZE can be evaluated faithfully, because it needs live per-strike OI.

For each signal we score the outcome directly on the option's OWN recorded premium path
using a 1R/2R bracket derived from the signal's stop distance:
  - target-1 (+1R) then target-2 (+2R), stop at the signal's SL (premium proxy)
  - if neither in HOLD, exit at last price in the hold window (EOD-style)
All PnL is in option-premium points x lot, so it reflects what the trader would actually
book on that specific contract.

Usage:
  python lab_services/backtest_recorded_ticks.py --date 2026-09-29 --inst SENSEX
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import argparse
import asyncio
from collections import defaultdict
import duckdb

from backend.strategies.oi_squeeze import OISqueezeSentinel
from backend.strategies.orb_breakout import VolumeBackedORB
from backend.strategies.vwap_ema import VWAPEMAAlignment
from backend.strategies.momentum_impulse import MomentumImpulseDetector
from backend.strategies.regime_filter import RegimeFilter
from backend.strategies.indicators import IncrementalADX, IncrementalVWAP, CandleAggregator

# Outcome model: risk-multiple bracket on the option premium.
# Stop = signal entry - (STOP_R * risk); target-2 = entry + (TARGET_R * risk).
# risk (premium pts) is inferred from the signal's spot SL mapped by a delta proxy, or a
# floor % of premium when no spot SL is present.
HOLD_SEC = 900.0          # 15 min max hold
DELTA_PROXY = 0.5         # spot->premium sensitivity for ATM
STOP_R = 1.0
TARGET1_R = 1.0
TARGET2_R = 2.0
MIN_RISK_PREM_PCT = 8.0   # floor: risk at least 8% of premium if SL-derived risk is tiny


def load_meta(date_str):
    with open(f"data/lake/date={date_str}/token_meta.json") as f:
        return json.load(f)


def load_ticks(date_str, inst, meta):
    glob = f"data/lake/date={date_str}/*.parquet"
    c = duckdb.connect()
    rows = c.execute(
        f"SELECT token, ltp, volume, open_interest, total_buy_qty, total_sell_qty, exchange_timestamp "
        f"FROM read_parquet('{glob}') ORDER BY exchange_timestamp ASC"
    ).df().to_dict("records")
    c.close()
    ticks = []
    for r in rows:
        tok = str(r["token"])
        m = meta.get(tok)
        if not m or m.get("name") != inst:
            continue
        ts = float(r["exchange_timestamp"]) / 1000.0
        ticks.append({
            "token": tok, "ltp": float(r["ltp"]), "volume": float(r.get("volume", 0.0)),
            "open_interest": float(r.get("open_interest", 0.0)),
            "total_buy_qty": float(r.get("total_buy_qty", 0.0)),
            "total_sell_qty": float(r.get("total_sell_qty", 0.0)),
            "exchange_timestamp": ts, "_meta": m,
        })
    return ticks


def build_price_index(ticks):
    idx = defaultdict(list)
    for t in ticks:
        if not t["_meta"].get("is_spot"):
            idx[t["token"]].append((t["exchange_timestamp"], t["ltp"]))
    return idx


def make_meta(m, inst, spot, strike_step):
    if m.get("is_spot"):
        return {"is_spot": True, "name": inst}
    off = 99
    if spot > 0:
        atm = round(spot / strike_step) * strike_step
        off = int(round((m["strike"] - atm) / strike_step))
    return {
        "name": inst, "option_type": m["option_type"], "strike": m["strike"],
        "lot_size": m.get("lot_size", 20 if inst == "SENSEX" else 65),
        "symbol": m.get("symbol", ""), "offset": off, "expiry": m.get("expiry"),
    }


def score(sig, price_idx, lot):
    """Resolve the signal on its option's forward premium path. Returns (outcome, pnl_inr)."""
    token = sig["option_token"]
    entry = sig["entry_price"]
    t0 = sig.get("_ts", 0.0)
    # risk in premium points
    sl_spot = sig.get("custom_sl_spot")
    risk = None
    if sl_spot:
        risk = abs(sig["spot_entry"] - sl_spot) * DELTA_PROXY
    if not risk or risk < entry * (MIN_RISK_PREM_PCT / 100.0):
        risk = entry * (MIN_RISK_PREM_PCT / 100.0)
    tgt1 = entry + TARGET1_R * risk
    tgt2 = entry + TARGET2_R * risk
    stop = entry - STOP_R * risk

    path = price_idx.get(token, [])
    half = lot // 2
    rest = lot - half
    t1_done = False
    cur_stop = stop
    last = entry
    for (ts, ltp) in path:
        if ts <= t0:
            continue
        if ts - t0 > HOLD_SEC:
            break
        last = ltp
        if not t1_done:
            if ltp <= cur_stop:
                pts = cur_stop - entry
                return "STOP", round(pts * lot, 0)
            if ltp >= tgt1:
                t1_done = True
                cur_stop = entry  # trail rest to breakeven
                if ltp >= tgt2:
                    pts1 = (tgt1 - entry) * half
                    pts2 = (tgt2 - entry) * rest
                    return "TARGET", round(pts1 + pts2, 0)
        else:
            if ltp >= tgt2:
                pts1 = (tgt1 - entry) * half
                pts2 = (tgt2 - entry) * rest
                return "TARGET", round(pts1 + pts2, 0)
            if ltp <= cur_stop:
                pts1 = (tgt1 - entry) * half
                return "PARTIAL_BE", round(pts1, 0)
    # EOD exit at last
    if t1_done:
        pts1 = (tgt1 - entry) * half
        pts2 = (last - entry) * rest
        return "EOD_AFTER_T1", round(pts1 + pts2, 0)
    return "EOD", round((last - entry) * lot, 0)


async def run_strategy(name, strat, ticks, price_idx, inst, strike_step, regime=None):
    """
    Replays ticks through the strategy. If `regime` (a RegimeFilter) is provided, it is
    driven live from spot ticks via 3-min candle aggregation (VWAP + ADX), mirroring the
    live warm-up path, so any regime gate inside the strategy sees real regime state.
    """
    signals = []
    spot_now = 0.0

    # Regime driver state (only used when regime is not None)
    agg = CandleAggregator(timeframe_seconds=180)
    vwap_ind = IncrementalVWAP()
    adx_ind = IncrementalADX(period=14)
    _regime_hist = {}

    for t in ticks:
        m = t["_meta"]
        meta = make_meta(m, inst, spot_now, strike_step)
        if m.get("is_spot"):
            spot_now = t["ltp"]
            if regime is not None:
                # Mirror the engine: feed the session net-change anchor on every spot tick.
                regime.update_session_reference(inst, t["ltp"], t["exchange_timestamp"],
                                                prev_close=t.get("_prev_close", 0.0) or 0.0)
                closed = agg.on_tick(t["exchange_timestamp"], t["ltp"], t.get("volume", 0.0) or 1.0)
                if closed:
                    tp = (closed["high"] + closed["low"] + closed["close"]) / 3.0
                    v = vwap_ind.update(tp, closed.get("volume", 1.0) or 1.0)
                    a = adx_ind.update(closed["high"], closed["low"], closed["close"])
                    st = regime.update_candle(inst, closed, v, a)
                    _regime_hist[st["regime"]] = _regime_hist.get(st["regime"], 0) + 1
        tick = {
            "token": t["token"], "ltp": t["ltp"], "volume": t["volume"],
            "open_interest": t["open_interest"], "total_buy_qty": t["total_buy_qty"],
            "total_sell_qty": t["total_sell_qty"], "exchange_timestamp": t["exchange_timestamp"],
        }
        sig = await strat.on_tick(tick, meta=meta)
        if sig:
            sig["_ts"] = t["exchange_timestamp"]
            signals.append(sig)

    wins = losses = 0
    pnl = 0.0
    ce_pnl = pe_pnl = 0.0
    ce_n = pe_n = 0
    rows = []
    for s in signals:
        lot = int(s.get("lot_size", 20))
        outcome, tr = score(s, price_idx, lot)
        if tr > 0:
            wins += 1
        elif tr < 0:
            losses += 1
        pnl += tr
        if s["direction"] == "CE":
            ce_pnl += tr; ce_n += 1
        else:
            pe_pnl += tr; pe_n += 1
        rows.append((s["direction"], s["strike"], round(s["entry_price"], 1),
                     s["details"].get("squeeze_type", s["details"].get("entry_type", "")),
                     outcome, int(tr), s.get("confidence", "")))
    return {"name": name, "n": len(signals), "wins": wins, "losses": losses,
            "pnl": int(pnl), "rows": rows,
            "ce": (ce_n, int(ce_pnl)), "pe": (pe_n, int(pe_pnl)),
            "regime_hist": _regime_hist}


def report(res):
    n = res["n"]
    decided = res["wins"] + res["losses"]
    wr = (res["wins"] / decided * 100.0) if decided else 0.0
    print(f"\n{'='*70}\n  {res['name']}\n{'='*70}")
    print(f"  Signals: {n} | Wins: {res['wins']} Losses: {res['losses']} "
          f"| WinRate(decided): {wr:.0f}% | NET PnL: INR {res['pnl']:+,}")
    if "ce" in res:
        print(f"  CE: {res['ce'][0]} trades, INR {res['ce'][1]:+,}  |  "
              f"PE: {res['pe'][0]} trades, INR {res['pe'][1]:+,}")
    if res["rows"]:
        print(f"  {'Dir':<5}{'Strike':<10}{'Entry':<9}{'Type':<16}{'Outcome':<14}{'PnL':>10}{'Conf':>6}")
        print("  " + "-" * 68)
        for r in res["rows"]:
            print(f"  {r[0]:<5}{r[1]:<10}{r[2]:<9}{str(r[3]):<16}{r[4]:<14}{r[5]:>10,}{str(r[6]):>6}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-09-29")
    ap.add_argument("--inst", default="SENSEX", choices=["NIFTY", "SENSEX"])
    args = ap.parse_args()

    strike_step = 100.0 if args.inst == "SENSEX" else 50.0
    meta = load_meta(args.date)
    print(f"Loading recorded ticks for {args.inst} on {args.date}...")
    ticks = load_ticks(args.date, args.inst, meta)
    print(f"Loaded {len(ticks)} ticks.")
    price_idx = build_price_index(ticks)

    # BEFORE: OI Squeeze with no regime gate (raw behavior)
    res_raw = await run_strategy("OI_SQUEEZE (no gate)", OISqueezeSentinel(None),
                                 ticks, price_idx, args.inst, strike_step)
    report(res_raw)

    # AFTER: OI Squeeze with the regime gate wired in, regime driven live from spot ticks
    regime = RegimeFilter(None)
    res_gated = await run_strategy("OI_SQUEEZE (regime-gated)",
                                   OISqueezeSentinel(None, regime_filter=regime),
                                   ticks, price_idx, args.inst, strike_step, regime=regime)
    report(res_gated)
    if res_gated.get("regime_hist"):
        print(f"  [regime distribution across 3-min candles]: {res_gated['regime_hist']}")

    print(f"\n{'='*70}")
    print(f"  DELTA (gate effect): signals {res_raw['n']} -> {res_gated['n']}, "
          f"PnL INR {res_raw['pnl']:+,} -> INR {res_gated['pnl']:+,}")
    print(f"{'='*70}")


if __name__ == "__main__":
    asyncio.run(main())
