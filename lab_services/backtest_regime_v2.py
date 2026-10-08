"""
REGIME FILTER v2 — TREND-QUALITY BACKTEST  (advisory-only, offline)
===================================================================
Replays recorded SPOT ticks through the SAME regime indicator stack the live engine uses
(CandleAggregator 180s -> IncrementalVWAP + IncrementalADX) and feeds BOTH:
  - v1  RegimeFilter        (live gating filter)
  - v2  RegimeFilterV2       with use_participation=False  (breadth is live-only; absent in lake)

For every closed 3m spot candle it records each filter's regime label, then resolves the label
against FORWARD spot movement over the next N bars to answer the only question that matters:

    When a filter says "TRENDING_{BULL|BEAR}", does price actually follow through in that
    direction over the next N bars, more than when it says NEUTRAL/CHOPPY?

Metrics per filter:
  - follow-through rate: of TRENDING_* bars, fraction whose forward move went the labelled way
  - avg forward move (in ATR units) conditioned on each regime label
  - transition count: how often the regime label flipped (stability; chop days should be high)
  - v1-vs-v2 agreement: fraction of bars where both agree TRENDING vs not-TRENDING

HONESTY: this measures the trend-quality dimension ONLY. Participation/breadth cannot be
backtested (no recorded constituent history). Follow-through is a necessary-but-not-sufficient
proxy for options profitability; it does NOT include option premium decay / entry mechanics.

Usage:
  python lab_services/backtest_regime_v2.py                 # all recorded days
  python lab_services/backtest_regime_v2.py 2026-10-06      # one day
  python lab_services/backtest_regime_v2.py 2026-10-06 2026-10-07 2026-10-08
"""
import os
import sys
import glob
import json
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyarrow.parquet as pq
from backend.strategies.regime_filter import RegimeFilter
from backend.strategies.regime_filter_v2 import RegimeFilterV2
from backend.strategies.indicators import CandleAggregator, IncrementalVWAP, IncrementalADX

LAKE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "lake")
SPOT_TOKENS = {"26000": "NIFTY", "99926000": "NIFTY", "99919000": "SENSEX", "26009": "SENSEX"}
FORWARD_BARS = 4  # resolve regime label against next 4 bars (~12 min) of spot movement


def list_days():
    return sorted(os.path.basename(d).split("=")[1]
                  for d in glob.glob(os.path.join(LAKE, "date=*")) if os.path.isdir(d))


def load_spot_rows(day):
    files = sorted(glob.glob(os.path.join(LAKE, f"date={day}", "ticks_*.parquet")))
    cols = ["token", "ltp", "volume", "exchange_timestamp"]
    rows = []
    for fp in files:
        have = set(pq.read_table(fp).schema.names)
        use = [c for c in cols if c in have]
        d = pq.read_table(fp, columns=use).to_pydict()
        for i in range(len(d["token"])):
            tok = str(d["token"][i])
            if tok not in SPOT_TOKENS:
                continue
            rows.append({
                "inst": SPOT_TOKENS[tok],
                "ltp": float(d["ltp"][i]),
                "volume": float(d.get("volume", [1.0] * len(d["token"]))[i] or 1.0),
                "ts": float(d["exchange_timestamp"][i]),
            })
    rows.sort(key=lambda r: r["ts"])
    return rows


def ts_sec(ms):
    s = float(ms)
    return s / 1000.0 if s > 1e11 else s


def build_candle_series(day):
    """Return {inst: [candle,...]} of closed 3m spot candles plus the regime inputs per candle."""
    rows = load_spot_rows(day)
    aggs = {"NIFTY": CandleAggregator(180), "SENSEX": CandleAggregator(180)}
    vwaps = {"NIFTY": IncrementalVWAP(), "SENSEX": IncrementalVWAP()}
    adxs = {"NIFTY": IncrementalADX(14), "SENSEX": IncrementalADX(14)}
    series = {"NIFTY": [], "SENSEX": []}

    for r in rows:
        inst = r["inst"]
        ts = ts_sec(r["ts"])
        ltp = r["ltp"]
        if ltp <= 0:
            continue
        vwaps[inst].update(ltp, r["volume"] or 1.0)
        closed = aggs[inst].on_tick(ts, ltp, volume=r["volume"] or 1.0)
        if closed:
            h = float(closed.get("high", ltp)); l = float(closed.get("low", ltp)); c = float(closed.get("close", ltp))
            vwap_val = vwaps[inst].value
            if c > 0 and abs(vwap_val - c) / c > 0.02:
                vwaps[inst].seed(c, 5000.0); vwap_val = c
            adx_val = adxs[inst].update(h, l, c)
            series[inst].append({
                "candle": closed, "vwap": vwap_val, "adx": adx_val,
                "plus_di": adxs[inst].plus_di, "minus_di": adxs[inst].minus_di,
                "atr": adxs[inst].tr_smooth or c * 0.003, "close": c,
            })
    return series


def resolve_forward(series, i, atr):
    """Signed forward move over FORWARD_BARS, in ATR units."""
    n = len(series)
    j = min(n - 1, i + FORWARD_BARS)
    if j <= i or atr <= 0:
        return None
    move = series[j]["close"] - series[i]["close"]
    return move / atr


def run_day(day, agg):
    series = build_candle_series(day)
    for inst in ("NIFTY", "SENSEX"):
        s = series[inst]
        if len(s) < FORWARD_BARS + 5:
            continue
        v1 = RegimeFilter()  # defaults
        v2 = RegimeFilterV2({"use_participation": False})
        prev1 = prev2 = None
        for i, row in enumerate(s):
            st1 = v1.update_candle(inst, row["candle"], row["vwap"], row["adx"])
            st2 = v2.update_candle(inst, row["candle"], row["vwap"], row["adx"],
                                   plus_di=row["plus_di"], minus_di=row["minus_di"],
                                   atr=row["atr"], breadth=None)
            fwd = resolve_forward(s, i, row["atr"])

            r1, r2 = st1.get("regime"), st2.get("regime")
            # transitions
            k = (inst,)
            if prev1 is not None and r1 != prev1:
                agg["v1_transitions"][inst] += 1
            if prev2 is not None and r2 != prev2:
                agg["v2_transitions"][inst] += 1
            prev1, prev2 = r1, r2

            if fwd is None:
                continue
            _tally(agg, "v1", inst, r1, st1.get("direction"), fwd)
            _tally(agg, "v2", inst, r2, st2.get("direction"), fwd)

            # agreement on trending-vs-not
            t1 = "TRENDING" in (r1 or ""); t2 = "TRENDING" in (r2 or "")
            agg["agree_bars"][inst] += 1
            if t1 == t2:
                agg["agree_trending"][inst] += 1


def _tally(agg, ver, inst, regime, direction, fwd):
    key = (ver, inst, _bucket(regime))
    agg["count"][key] += 1
    agg["fwd_sum"][key] += fwd
    agg["fwd_abs_sum"][key] += abs(fwd)
    if "TRENDING" in (regime or ""):
        want = 1 if direction == "BULL" else (-1 if direction == "BEAR" else 0)
        if want != 0:
            agg["trend_n"][(ver, inst)] += 1
            if (fwd > 0 and want > 0) or (fwd < 0 and want < 0):
                agg["trend_hit"][(ver, inst)] += 1


def _bucket(regime):
    r = regime or "NA"
    if "TRENDING" in r:
        return "TRENDING"
    return r  # NEUTRAL / CHOPPY / NA


def main():
    days = sys.argv[1:] or list_days()
    agg = {
        "count": defaultdict(int), "fwd_sum": defaultdict(float), "fwd_abs_sum": defaultdict(float),
        "trend_n": defaultdict(int), "trend_hit": defaultdict(int),
        "v1_transitions": defaultdict(int), "v2_transitions": defaultdict(int),
        "agree_bars": defaultdict(int), "agree_trending": defaultdict(int),
    }
    used = []
    for day in days:
        try:
            run_day(day, agg)
            used.append(day)
        except Exception as e:
            print(f"  ! {day}: {e}")

    print("=" * 78)
    print(f"REGIME v2 TREND-QUALITY BACKTEST  (days: {', '.join(used)})")
    print(f"forward horizon = {FORWARD_BARS} bars (~{FORWARD_BARS*3}min), move in ATR units")
    print("=" * 78)

    for inst in ("NIFTY", "SENSEX"):
        print(f"\n### {inst}")
        print(f"{'ver':4} {'regime':10} {'n':>5} {'avg_fwd(ATR)':>13} {'avg|fwd|(ATR)':>14}")
        for ver in ("v1", "v2"):
            for bucket in ("TRENDING", "NEUTRAL", "CHOPPY", "NA"):
                key = (ver, inst, bucket)
                n = agg["count"][key]
                if n == 0:
                    continue
                avg = agg["fwd_sum"][key] / n
                avga = agg["fwd_abs_sum"][key] / n
                print(f"{ver:4} {bucket:10} {n:>5} {avg:>13.3f} {avga:>14.3f}")
        for ver in ("v1", "v2"):
            tn = agg["trend_n"][(ver, inst)]; th = agg["trend_hit"][(ver, inst)]
            rate = (th / tn * 100.0) if tn else 0.0
            print(f"  {ver} TRENDING follow-through: {th}/{tn} = {rate:.1f}%")
        print(f"  transitions  v1={agg['v1_transitions'][inst]}  v2={agg['v2_transitions'][inst]}")
        ab = agg["agree_bars"][inst]; at = agg["agree_trending"][inst]
        print(f"  v1/v2 trending-agreement: {at}/{ab} = {(at/ab*100.0) if ab else 0:.1f}%")

    print("\nNOTE: trend-quality only; breadth/participation is live-only and NOT in this test.")
    print("Follow-through is a directional proxy, not option-premium PnL.")


if __name__ == "__main__":
    main()
