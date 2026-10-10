"""
SPOT-HISTORY BACKTEST  (API-fetched, no local ticks)  — advisory-only, offline
==============================================================================
For a date with NO recorded tick lake (e.g. 2026-09-01), fetches NIFTY/SENSEX index SPOT 3-minute
OHLC from AngelOne getCandleData and runs the SPOT-DRIVEN layer only:
  - RegimeFilter v1 (live gating filter)
  - RegimeFilterV2 (recalibrated A/B candidate, loaded from market_rules.yaml)
and characterizes the session: regime time-in-state, transitions, and DIRECTIONAL FOLLOW-THROUGH
of each regime label against forward SPOT movement (ATR units, 4-bar horizon).

SCOPE / HONESTY — read this before trusting any number:
  * This is SPOT-ONLY. getCandleData returns index OHLC; it does NOT return per-strike option
    premium ticks. The trade strategies (OI Squeeze / ORB / VWAP_EMA / Momentum) gate on and
    resolve P&L against the OPTION premium path, which does not exist for an unrecorded day.
    Therefore this script produces NO strategy signals and NO P&L — doing so would be fabricated.
  * What it DOES give: an honest read of the regime/trend structure of that real session and how
    the two regime filters would have labelled it. Breadth (v2 participation) is unavailable for a
    historical day, so v2 runs with use_participation disabled.
  * Index volume is 0 from this API, as expected; volume-based logic is not exercised.

Usage:
  python lab_services/backtest_spot_history.py 2026-09-01
  python lab_services/backtest_spot_history.py 2026-09-01 --inst NIFTY
"""
import os, sys, asyncio, yaml
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.core.auth import AngelOneAuth
from backend.strategies.regime_filter import RegimeFilter
from backend.strategies.regime_filter_v2 import RegimeFilterV2
from backend.strategies.indicators import IncrementalVWAP, IncrementalADX

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKENS = {"NIFTY": ("NSE", "99926000"), "SENSEX": ("BSE", "99919000")}
FORWARD_BARS = 4


def load_v2_cfg():
    p = os.path.join(ROOT, "config", "market_rules.yaml")
    cfg = yaml.safe_load(open(p))["strategies"].get("regime_filter_v2", {}) or {}
    cfg = dict(cfg)
    cfg["use_participation"] = False  # no breadth for a historical day
    return cfg


def fetch_candles(sc, inst, day):
    exch, token = TOKENS[inst]
    param = {"exchange": exch, "symboltoken": token, "interval": "THREE_MINUTE",
             "fromdate": f"{day} 09:15", "todate": f"{day} 15:30"}
    res = sc.getCandleData(param)
    data = (res or {}).get("data") or []
    out = []
    for row in data:
        # [timestamp, open, high, low, close, volume]
        out.append({"ts": row[0], "open": float(row[1]), "high": float(row[2]),
                    "low": float(row[3]), "close": float(row[4]), "volume": float(row[5])})
    return out


def analyze(inst, candles, v2_cfg):
    if len(candles) < FORWARD_BARS + 5:
        print(f"\n### {inst}: only {len(candles)} candles — insufficient")
        return
    v1 = RegimeFilter()
    v2 = RegimeFilterV2(v2_cfg)
    vwap = IncrementalVWAP()
    adx = IncrementalADX(14)

    states = []
    for c in candles:
        h, l, cl = c["high"], c["low"], c["close"]
        # index has no real volume; use a constant so VWAP is a simple typical-price average
        vwap.update((h + l + cl) / 3.0, 1000.0)
        a = adx.update(h, l, cl)
        vv = vwap.value
        if cl > 0 and abs(vv - cl) / cl > 0.02:
            vwap.seed(cl, 5000.0); vv = cl
        st1 = v1.update_candle(inst, c, vv, a)
        st2 = v2.update_candle(inst, c, vv, a, plus_di=adx.plus_di, minus_di=adx.minus_di,
                               atr=adx.tr_smooth or cl * 0.003, breadth=None)
        states.append({"c": cl, "atr": adx.tr_smooth or cl * 0.003,
                       "v1": st1["regime"], "v1dir": st1.get("direction"),
                       "v2": st2["regime"], "v2dir": st2.get("direction")})

    def bucket(r):
        return "TRENDING" if "TRENDING" in (r or "") else (r or "NA")

    def summarize(key, dirkey):
        tally = defaultdict(int); trans = 0; prev = None
        hit = tot = 0
        for i, s in enumerate(states):
            b = bucket(s[key]); tally[b] += 1
            if prev is not None and s[key] != prev:
                trans += 1
            prev = s[key]
            j = min(len(states) - 1, i + FORWARD_BARS)
            atr = s["atr"]
            if j > i and atr > 0 and b == "TRENDING":
                fwd = (states[j]["c"] - s["c"]) / atr
                want = 1 if s[dirkey] == "BULL" else -1
                tot += 1
                if (fwd > 0 and want > 0) or (fwd < 0 and want < 0):
                    hit += 1
        return tally, trans, hit, tot

    net = candles[-1]["close"] - candles[0]["open"]
    hi = max(c["high"] for c in candles); lo = min(c["low"] for c in candles)
    print(f"\n### {inst}  —  {len(candles)} candles")
    print(f"  open {candles[0]['open']:.1f}  close {candles[-1]['close']:.1f}  "
          f"net {net:+.1f} ({net/candles[0]['open']*100:+.2f}%)  range {hi-lo:.0f}pts")
    for key, dk, label in (("v1", "v1dir", "v1 (live)"), ("v2", "v2dir", "v2 (recalibrated)")):
        tally, trans, hit, tot = summarize(key, dk)
        order = sorted(tally.items(), key=lambda x: -x[1])
        ft = f"{hit}/{tot} = {hit/tot*100:.0f}%" if tot else "n/a"
        print(f"  {label:18} " + ", ".join(f"{k} {v}" for k, v in order)
              + f"  | transitions {trans} | TRENDING follow-through {ft}")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("usage: backtest_spot_history.py YYYY-MM-DD [--inst NIFTY|SENSEX]")
        return
    day = args[0]
    only = None
    if "--inst" in sys.argv:
        only = sys.argv[sys.argv.index("--inst") + 1].upper()

    auth = AngelOneAuth()
    res = asyncio.run(auth.login())
    if not res.get("status"):
        print(f"Auth failed: {res.get('message')} — cannot fetch historical candles.")
        return
    sc = auth.smart_connect
    v2_cfg = load_v2_cfg()

    print("=" * 78)
    print(f"SPOT-HISTORY BACKTEST  —  {day}  (SPOT-ONLY: regime structure, no option P&L)")
    print("=" * 78)
    insts = [only] if only else ["NIFTY", "SENSEX"]
    for inst in insts:
        try:
            candles = fetch_candles(sc, inst, day)
        except Exception as e:
            print(f"\n### {inst}: fetch error: {e}")
            continue
        if not candles:
            print(f"\n### {inst}: no candles returned for {day} (holiday / out of history window)")
            continue
        analyze(inst, candles, v2_cfg)

    print("\nSCOPE: spot-only. No per-strike option data exists for an unrecorded day, so no")
    print("strategy signals or P&L are produced. This is the regime/trend structure only.")


if __name__ == "__main__":
    main()
