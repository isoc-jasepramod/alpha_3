"""
All-strategy backtest on recorded tick data (advisory-only, offline).

Replays a recorded day through the REAL trade strategies (OI Squeeze, Volume-Backed ORB,
VWAP_EMA, Momentum Impulse) wired exactly as the live server:
  - warms indicators + the independent RegimeFilter from the day's own 3m candles,
  - drives the regime filter from spot ticks (mirrors _drive_regime),
  - feeds spot + option ticks to every strategy,
  - resolves each emitted signal via the REAL RiskGovernor bracket on the option's forward path.

Offset is computed per tick from current spot vs strike. NOTE: this is only reliable on days
where recorded strikes track spot through the session; days with gap-pinned subscriptions
(strikes far from traded spot) will under-report option-gated signals — run only on clean days.

Usage:
  python lab_services/backtest_all_strategies.py 2026-10-01
  python lab_services/backtest_all_strategies.py            # all days
"""
import os, sys, glob, json, bisect, asyncio
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyarrow.parquet as pq
from backend.strategies.oi_squeeze import OISqueezeSentinel
from backend.strategies.orb_breakout import VolumeBackedORB
from backend.strategies.vwap_ema import VWAPEMAAlignment
from backend.strategies.momentum_impulse import MomentumImpulseDetector
from backend.strategies.regime_filter import RegimeFilter
from backend.risk.risk_governor import RiskGovernor
from backend.strategies.indicators import CandleAggregator, IncrementalVWAP, IncrementalADX

LAKE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "lake")
SPOT = {"26000": "NIFTY", "99926000": "NIFTY", "99919000": "SENSEX"}
STEP = {"NIFTY": 50.0, "SENSEX": 100.0}


def list_days():
    return sorted(os.path.basename(d).split("=")[1]
                  for d in glob.glob(os.path.join(LAKE, "date=*")) if os.path.isdir(d))


def load_meta(day):
    p = os.path.join(LAKE, f"date={day}", "token_meta.json")
    return json.load(open(p)) if os.path.exists(p) else {}


def load_rows(day):
    rows = []
    for fp in sorted(glob.glob(os.path.join(LAKE, f"date={day}", "ticks_*.parquet"))):
        have = set(pq.read_table(fp).schema.names)
        cols = [c for c in ["token", "ltp", "volume", "open_interest",
                            "total_buy_qty", "total_sell_qty", "exchange_timestamp"] if c in have]
        d = pq.read_table(fp, columns=cols).to_pydict()
        for i in range(len(d["token"])):
            rows.append({c: d[c][i] for c in cols})
    rows.sort(key=lambda r: r["exchange_timestamp"])
    return rows


def tsec(ms):
    s = float(ms)
    return s / 1000.0 if s > 1e11 else s


def warm_candles(rows):
    """Rebuild 3m spot candles per instrument from the day's own recorded ticks (for warmup)."""
    sticks = defaultdict(list)
    for r in rows:
        tok = str(r["token"])
        if tok in SPOT:
            lt = float(r.get("ltp", 0.0) or 0.0)
            if lt > 0:
                sticks[SPOT[tok]].append((tsec(r["exchange_timestamp"]), lt))
    out = {}
    for inst, tk in sticks.items():
        tk.sort()
        agg = CandleAggregator(180)
        c = []
        for ts, lt in tk:
            cc = agg.on_tick(ts, lt, volume=1000.0)
            if cc:
                c.append(cc)
        out[inst] = c
    return out


async def run_day(day):
    meta = load_meta(day)
    rows = load_rows(day)
    if not meta or not rows:
        return {}
    warm = warm_candles(rows)

    regime = RegimeFilter()
    oi = OISqueezeSentinel()
    orb = VolumeBackedORB()
    vwap = VWAPEMAAlignment(regime_filter=regime)
    mom = MomentumImpulseDetector(regime_filter=regime)
    gov = RiskGovernor()
    strategies = {"OI_SQUEEZE": oi, "ORB": orb, "VWAP_EMA": vwap, "MOMENTUM": mom}

    # independent regime feed (mirror server _drive_regime)
    r_agg = {"NIFTY": CandleAggregator(180), "SENSEX": CandleAggregator(180)}
    r_vwap = {"NIFTY": IncrementalVWAP(), "SENSEX": IncrementalVWAP()}
    r_adx = {"NIFTY": IncrementalADX(14), "SENSEX": IncrementalADX(14)}

    # warm everything
    for inst in ("NIFTY", "SENSEX"):
        cands = warm.get(inst, [])
        regime.seed_from_candles(inst, cands)
        for s in strategies.values():
            if hasattr(s, "seed_from_candles"):
                try:
                    s.seed_from_candles(inst, cands)
                except Exception:
                    pass
        for c in cands:
            h, l, cl, v = c["high"], c["low"], c["close"], c.get("volume", 1000.0)
            if cl <= 0:
                continue
            r_vwap[inst].update((h + l + cl) / 3.0, v)
            r_adx[inst].update(h, l, cl)

    gov_bar = {"NIFTY": CandleAggregator(60), "SENSEX": CandleAggregator(60)}
    spot = {"NIFTY": 0.0, "SENSEX": 0.0}
    price_tl = defaultdict(list)
    signals = []

    for r in rows:
        tok = str(r.get("token", ""))
        ltp = float(r.get("ltp", 0.0) or 0.0)
        if ltp <= 0:
            continue
        t = tsec(r["exchange_timestamp"])
        m = meta.get(tok)

        if tok in SPOT or (m and m.get("is_spot")):
            inst = SPOT.get(tok) or m.get("name", "NIFTY")
            spot[inst] = ltp
            # drive independent regime
            bar = r_agg[inst].on_tick(t, ltp, volume=1.0)
            if bar:
                vw = r_vwap[inst].value or bar["close"]
                av = r_adx[inst].update(bar["high"], bar["low"], bar["close"])
                regime.update_candle(inst, bar, vw, av)
            gb = gov_bar[inst].on_tick(t, ltp, volume=1.0)
            if gb:
                gov.update_spot_bar(inst, gb["high"], gb["low"], gb["close"])
            smeta = {"is_spot": True, "name": inst}
            stick = {"token": tok, "ltp": ltp, "volume": float(r.get("volume", 0.0) or 0.0),
                     "exchange_timestamp": r["exchange_timestamp"]}
            for s in strategies.values():
                await s.on_tick(stick, smeta)
            continue

        if not m or not m.get("option_type"):
            continue
        price_tl[tok].append((t, ltp))
        inst = m.get("name", "NIFTY")
        strike = float(m.get("strike", 0.0))
        stp = STEP.get(inst, 50.0)
        sp = spot.get(inst, 0.0)
        off = int(round((strike - round(sp / stp) * stp) / stp)) if (sp > 0 and strike > 0) else None
        ometa = {"name": inst, "is_spot": False, "option_type": m.get("option_type"),
                 "strike": strike, "lot_size": int(m.get("lot_size", 50) or 50),
                 "symbol": m.get("symbol", tok), "offset": off, "expiry": m.get("expiry", "")}
        otick = {"token": tok, "ltp": ltp, "volume": float(r.get("volume", 0.0) or 0.0),
                 "open_interest": float(r.get("open_interest", 0.0) or 0.0),
                 "total_buy_qty": float(r.get("total_buy_qty", 0.0) or 0.0),
                 "total_sell_qty": float(r.get("total_sell_qty", 0.0) or 0.0),
                 "exchange_timestamp": r["exchange_timestamp"]}
        for name, s in strategies.items():
            cand = await s.on_tick(otick, ometa)
            if cand:
                fin = gov.evaluate_signal(cand)
                if fin:
                    signals.append({"strategy": name, "token": tok, "dir": cand["direction"],
                                    "ets": t, "entry": float(fin["entry_price"]),
                                    "stop": float(fin["stop_loss"]), "target": float(fin["target"])})

    # resolve on option forward path
    tl_ts = {k: [x[0] for x in v] for k, v in price_tl.items()}
    tl_px = {k: [x[1] for x in v] for k, v in price_tl.items()}
    for s in signals:
        a = tl_ts.get(s["token"], [])
        px = tl_px.get(s["token"], [])
        st = bisect.bisect_right(a, s["ets"])
        out, ex = "EOD", s["entry"]
        for i in range(st, len(px)):
            p = px[i]
            if p <= s["stop"]:
                out, ex = "STOP", s["stop"]; break
            if p >= s["target"]:
                out, ex = "TARGET", s["target"]; break
        else:
            if px and st < len(px):
                ex = px[-1]
        s["out"] = out
        s["ret"] = (ex - s["entry"]) / s["entry"] * 100.0

    # aggregate per strategy
    agg = {}
    for name in strategies:
        grp = [s for s in signals if s["strategy"] == name]
        agg[name] = grp
    return agg


def fmt(grp, label):
    if not grp:
        return f"  {label:12s}: 0 signals"
    n = len(grp)
    ce = sum(1 for s in grp if s["dir"] == "CE")
    pe = n - ce
    tgt = sum(1 for s in grp if s["out"] == "TARGET")
    stop = sum(1 for s in grp if s["out"] == "STOP")
    avg = sum(s["ret"] for s in grp) / n
    return (f"  {label:12s}: n={n:>3}  CE/PE={ce}/{pe}  TARGET={tgt} STOP={stop}  "
            f"avg_ret={avg:+6.2f}%/trade")


async def main():
    days = [sys.argv[1]] if len(sys.argv) > 1 else list_days()
    totals = defaultdict(list)
    for day in days:
        agg = await run_day(day)
        if not agg:
            continue
        day_total = sum(len(v) for v in agg.values())
        print(f"\n===== {day} =====  ({day_total} signals)")
        for name in ("OI_SQUEEZE", "ORB", "VWAP_EMA", "MOMENTUM"):
            print(fmt(agg.get(name, []), name))
            totals[name] += agg.get(name, [])
    print("\n================ AGGREGATE ================")
    for name in ("OI_SQUEEZE", "ORB", "VWAP_EMA", "MOMENTUM"):
        print(fmt(totals[name], name))
    allsig = [s for g in totals.values() for s in g]
    if allsig:
        avg = sum(s["ret"] for s in allsig) / len(allsig)
        print(f"\n  OVERALL: {len(allsig)} signals  avg_ret={avg:+.2f}%/trade")
    print("\nResolved on option premium to the governor 2R target / ~20%-floor stop.")
    print("Reliable only on days where recorded strikes track spot (clean: 2026-09-29/30, 2026-10-01).")


if __name__ == "__main__":
    asyncio.run(main())
