"""
REGIME v2 CALIBRATION HARNESS  (offline)
========================================
Diagnoses and re-tunes RegimeFilterV2's trend-quality scoring using recorded data.

Root problems found in the Oct 9 live A/B:
  1. Efficiency Ratio (ER) at weight 0.30 single-handedly vetoed healthy trends: when a trend
     pauses mid-move, ER collapses while ADX/DI/breadth/slope all still confirm. 14/18 of the
     NIFTY demotions had ER<0.25 with ADX>=25.
  2. The 65 trending-threshold was inherited from v1 but v2 scores on a different scale; demoted
     candles clustered at score 61-62, just under the line.

This harness recomputes v2's component sub-scores per 3m spot candle across all recorded days,
replays the LIVE breadth for 2026-10-09 from the regime_compare JSONL (so it matches live), and
sweeps candidate configs. Each config is scored on TWO objectives using forward spot movement
(ATR units, 4-bar horizon) as the outcome proxy:

  TREND-CAPTURE : of bars whose forward move was a strong directional follow-through
                  (|fwd| >= 1.0 ATR), what fraction did the config label TRENDING in the
                  correct direction?  (higher = better; we want to KEEP real trends)
  CHOP-REJECTION: of bars labelled TRENDING, what fraction actually followed through
                  (directional hit-rate)?  (higher = fewer false-trend calls)

We also report, per config, how many of the Oct-9 "healthy trend demoted to NEUTRAL" candles it
RECOVERS (i.e. now correctly labels TRENDING) — the specific live failure we're fixing.

Honest scope: trend-quality + breadth(only where recorded). Forward-move is a directional proxy,
NOT option-premium PnL. Thresholds tuned here MUST be re-confirmed before any gating.

Usage:
  python lab_services/calibrate_regime_v2.py            # diagnose current + sweep
"""
import os, sys, glob, json
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyarrow.parquet as pq
from backend.strategies.indicators import CandleAggregator, IncrementalVWAP, IncrementalADX

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAKE = os.path.join(ROOT, "data", "lake")
COMPARE = os.path.join(ROOT, "data", "regime_compare")
SPOT = {"26000": "NIFTY", "99926000": "NIFTY", "99919000": "SENSEX", "26009": "SENSEX"}
FORWARD_BARS = 4

# Day classification (from prior analysis) for reporting context.
DAY_TYPE = {
    "2026-10-06": "trend-up",
    "2026-10-07": "chop",
    "2026-10-08": "trend-down",
    "2026-10-09": "trend-up(live-breadth)",
}


# ---------- pure component scorers (mirror RegimeFilterV2) ----------
def adx_score(adx, prev_adx):
    if adx <= 15.0:
        base = max(0.0, adx) / 15.0 * 30.0
    elif adx <= 25.0:
        base = 30.0 + (adx - 15.0) / 10.0 * 35.0
    elif adx <= 40.0:
        base = 65.0 + (adx - 25.0) / 15.0 * 25.0
    else:
        base = 90.0 + min(10.0, adx - 40.0)
    bonus = 0.0
    if prev_adx is not None:
        d = adx - prev_adx
        bonus = min(15.0, 8.0 + d * 5.0) if d > 0.1 else (-5.0 if d < -0.2 else 0.0)
    return max(0.0, min(100.0, base + bonus))


def er_score(closes):
    if len(closes) < 3:
        return 0.5, 50.0
    net = abs(closes[-1] - closes[0])
    tot = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    if tot <= 1e-5:
        return 0.0, 0.0
    er = max(0.0, min(1.0, net / tot))
    if er < 0.20:
        s = er / 0.20 * 20.0
    elif er < 0.40:
        s = 20.0 + (er - 0.20) / 0.20 * 30.0
    elif er < 0.70:
        s = 50.0 + (er - 0.40) / 0.30 * 35.0
    else:
        s = 85.0 + min(15.0, (er - 0.70) / 0.30 * 15.0)
    return er, max(0.0, min(100.0, s))


def persistence_score(candles, vwaps):
    n = min(len(candles), len(vwaps))
    if n < 3:
        return 50.0
    sides, nd = [], []
    above = below = 0
    for i in range(-n, 0):
        c = candles[i]["close"]; v = vwaps[i]
        if v <= 0:
            continue
        side = 1 if c >= v else -1
        sides.append(side); above += side == 1; below += side == -1
        nd.append(min(abs(c - v) / v, 0.01) / 0.01)
    if not sides:
        return 50.0
    dom = max(above, below) / len(sides)
    avg = sum(nd) / len(nd)
    raw = max(0.0, (dom - 0.5) / 0.5) * 100.0
    ds = raw * (0.5 + 0.5 * avg)
    crosses = sum(1 for i in range(1, len(sides)) if sides[i] != sides[i - 1])
    pen = min(30.0, (crosses - 2) * 10.0) if crosses >= 3 else 0.0
    return max(0.0, min(100.0, ds - pen))


def slope_norm_score(vwaps, atr, target):
    if len(vwaps) < 2 or atr <= 0:
        return 0.0, 50.0
    k = max(1, len(vwaps) - 1)
    slope = (vwaps[-1] - vwaps[0]) / k
    sn = slope / atr
    return sn, min(100.0, abs(sn) / max(0.01, target) * 100.0)


# ---------- data ----------
def load_spot(day):
    rows = []
    for fp in sorted(glob.glob(os.path.join(LAKE, f"date={day}", "ticks_*.parquet"))):
        have = set(pq.read_table(fp).schema.names)
        cols = [c for c in ("token", "ltp", "volume", "exchange_timestamp") if c in have]
        d = pq.read_table(fp, columns=cols).to_pydict()
        for i in range(len(d["token"])):
            t = str(d["token"][i])
            if t not in SPOT:
                continue
            rows.append((float(d["exchange_timestamp"][i]), SPOT[t], float(d["ltp"][i]),
                         float((d.get("volume") or [1])[i] or 1)))
    rows.sort(key=lambda r: r[0])
    return rows


def load_live_breadth(day, inst):
    """Return list of breadth dicts in candle order from the recorded compare JSONL, if present."""
    p = os.path.join(COMPARE, f"date={day}", f"{inst}.jsonl")
    if not os.path.exists(p):
        return None
    out = []
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line).get("breadth"))
        except json.JSONDecodeError:
            out.append(None)
    return out


def tsec(x):
    return x / 1000.0 if x > 1e11 else x


def build_series(day):
    """{inst: [ {close, adx, prev_adx, plus_di, minus_di, atr, candles[], vwaps[]} ... ]}
    Pre-computes everything config-independent so sweeps are cheap."""
    rows = load_spot(day)
    aggs = {"NIFTY": CandleAggregator(180), "SENSEX": CandleAggregator(180)}
    vwaps = {"NIFTY": IncrementalVWAP(), "SENSEX": IncrementalVWAP()}
    adxs = {"NIFTY": IncrementalADX(14), "SENSEX": IncrementalADX(14)}
    chist = {"NIFTY": [], "SENSEX": []}
    vhist = {"NIFTY": [], "SENSEX": []}
    series = {"NIFTY": [], "SENSEX": []}
    prev_adx = {"NIFTY": None, "SENSEX": None}
    for ts, inst, ltp, vol in rows:
        if ltp <= 0:
            continue
        vwaps[inst].update(ltp, vol or 1)
        c = aggs[inst].on_tick(tsec(ts), ltp, volume=vol or 1)
        if not c:
            continue
        h = float(c["high"]); l = float(c["low"]); cl = float(c["close"])
        vv = vwaps[inst].value
        if cl > 0 and abs(vv - cl) / cl > 0.02:
            vwaps[inst].seed(cl, 5000.0); vv = cl
        a = adxs[inst].update(h, l, cl)
        chist[inst].append(c); vhist[inst].append(vv)
        series[inst].append({
            "close": cl, "adx": a, "prev_adx": prev_adx[inst],
            "plus_di": adxs[inst].plus_di, "minus_di": adxs[inst].minus_di,
            "atr": adxs[inst].tr_smooth or cl * 0.003, "vwap": vv,
            "candles": list(chist[inst]), "vwaps": list(vhist[inst]),
        })
        prev_adx[inst] = a
    return series


def classify(row, cfg, breadth):
    """Return (regime_bucket, direction, score) under a candidate config dict."""
    a_s = adx_score(row["adx"], row["prev_adx"])
    er_v, e_s = er_score([c["close"] for c in row["candles"][-cfg["er_lb"]:]])
    p_s = persistence_score(row["candles"][-cfg["pers_lb"]:], row["vwaps"][-cfg["pers_lb"]:])
    sn, sl_s = slope_norm_score(row["vwaps"][-cfg["slope_lb"]:], row["atr"], cfg["slope_target"])
    tq = max(0.0, min(100.0,
        cfg["w_adx"] * a_s + cfg["w_er"] * e_s + cfg["w_pers"] * p_s + cfg["w_slope"] * sl_s))

    part = 0.0
    b_pct = None
    if cfg["use_part"] and breadth and breadth.get("valid"):
        b_pct = breadth.get("breadth_pct")
        if b_pct is not None:
            b_bias = (b_pct - 0.5) * 2.0
            price_dir = 1 if row["close"] >= row["vwap"] else -1
            part = (b_bias * price_dir) * (cfg["part_w"] * 100.0)
            if breadth.get("spoof_detected"):
                part -= 10.0
    score = max(0.0, min(100.0, tq + part))

    # direction
    pdi, mdi = row["plus_di"], row["minus_di"]
    di_dir = 1 if pdi > mdi else (-1 if mdi > pdi else 0)
    price_dir = 1 if row["close"] >= row["vwap"] else -1
    slope_dir = 1 if sn > 0 else (-1 if sn < 0 else 0)
    bdir = 0
    if b_pct is not None:
        bdir = 1 if b_pct >= 0.55 else (-1 if b_pct <= 0.45 else 0)
    vote = 2 * di_dir + 1.5 * price_dir + 1.0 * slope_dir + 1.0 * bdir
    direction = "BULL" if vote > 0 else ("BEAR" if vote < 0 else ("BULL" if price_dir > 0 else "BEAR"))

    # DI + breadth override: a strongly-confirmed trend must not be demoted by low ER alone.
    di_spread = abs(pdi - mdi)
    override = False
    if cfg.get("di_breadth_override"):
        strong_di = di_spread >= cfg.get("override_di_spread", 12.0)
        strong_breadth = (b_pct is not None and (b_pct >= 0.65 or b_pct <= 0.35))
        aligned = (di_dir == price_dir) and (bdir == 0 or bdir == di_dir)
        if strong_di and strong_breadth and aligned and row["adx"] >= cfg.get("override_min_adx", 22.0):
            override = True

    if score >= cfg["trend_th"] or override:
        return "TRENDING", direction, score
    elif score >= cfg["neutral_th"]:
        return "NEUTRAL", "NEUTRAL", score
    return "CHOPPY", "NEUTRAL", score


def fwd_move(series, i):
    j = min(len(series) - 1, i + FORWARD_BARS)
    atr = series[i]["atr"]
    if j <= i or atr <= 0:
        return None
    return (series[j]["close"] - series[i]["close"]) / atr


CONFIGS = {
    "current (w_er=0.30, th=65, no-override)": dict(
        w_adx=0.30, w_er=0.30, w_pers=0.25, w_slope=0.15, er_lb=10, pers_lb=10, slope_lb=5,
        slope_target=0.15, use_part=True, part_w=0.20, trend_th=65, neutral_th=45,
        di_breadth_override=False),
    "A: w_er 0.15, th 60": dict(
        w_adx=0.37, w_er=0.15, w_pers=0.28, w_slope=0.20, er_lb=10, pers_lb=10, slope_lb=5,
        slope_target=0.15, use_part=True, part_w=0.20, trend_th=60, neutral_th=42,
        di_breadth_override=False),
    "B: w_er 0.15, th 60, +override": dict(
        w_adx=0.37, w_er=0.15, w_pers=0.28, w_slope=0.20, er_lb=10, pers_lb=10, slope_lb=5,
        slope_target=0.15, use_part=True, part_w=0.20, trend_th=60, neutral_th=42,
        di_breadth_override=True, override_di_spread=12.0, override_min_adx=22.0),
    "C: w_er 0.20, th 62, +override": dict(
        w_adx=0.33, w_er=0.20, w_pers=0.27, w_slope=0.20, er_lb=10, pers_lb=10, slope_lb=5,
        slope_target=0.15, use_part=True, part_w=0.20, trend_th=62, neutral_th=44,
        di_breadth_override=True, override_di_spread=12.0, override_min_adx=22.0),
    "D: w_er 0.10, th 58, +override": dict(
        w_adx=0.40, w_er=0.10, w_pers=0.28, w_slope=0.22, er_lb=10, pers_lb=10, slope_lb=5,
        slope_target=0.15, use_part=True, part_w=0.20, trend_th=58, neutral_th=40,
        di_breadth_override=True, override_di_spread=10.0, override_min_adx=20.0),
    "E: w_er 0.18, th 62 (A-C mid)": dict(
        w_adx=0.35, w_er=0.18, w_pers=0.27, w_slope=0.20, er_lb=10, pers_lb=10, slope_lb=5,
        slope_target=0.15, use_part=True, part_w=0.20, trend_th=62, neutral_th=44,
        di_breadth_override=False),
    "F: w_er 0.18, th 60": dict(
        w_adx=0.35, w_er=0.18, w_pers=0.27, w_slope=0.20, er_lb=10, pers_lb=10, slope_lb=5,
        slope_target=0.15, use_part=True, part_w=0.20, trend_th=60, neutral_th=43,
        di_breadth_override=False),
}


def main():
    days = [d for d in ("2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09")
            if os.path.isdir(os.path.join(LAKE, f"date={d}"))]
    # Pre-build series + breadth once
    data = {}
    for day in days:
        s = build_series(day)
        br = {inst: load_live_breadth(day, inst) for inst in ("NIFTY", "SENSEX")}
        data[day] = (s, br)

    print("=" * 90)
    print("REGIME v2 CALIBRATION  —  trend-capture vs chop-rejection across recorded day-types")
    print(f"forward horizon {FORWARD_BARS} bars (~{FORWARD_BARS*3}min), outcome in ATR units")
    print("=" * 90)

    # Oct-9 live demotions (v1 TRENDING but v2 NEUTRAL/CHOPPY) — the specific failure to fix.
    # Index them by candle position so we can check if a config now recovers them.
    live_demotions = {"NIFTY": set(), "SENSEX": set()}
    for inst in ("NIFTY", "SENSEX"):
        p = os.path.join(COMPARE, "date=2026-10-09", f"{inst}.jsonl")
        if os.path.exists(p):
            for idx, line in enumerate(open(p, encoding="utf-8")):
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                v1t = "TRENDING" in (r["v1"].get("regime") or "")
                v2t = "TRENDING" in (r["v2"].get("regime") or "")
                if v1t and not v2t:
                    live_demotions[inst].add(idx)

    for name, cfg in CONFIGS.items():
        # aggregate across days/instruments
        strong_total = strong_captured = 0   # trend-capture
        trend_total = trend_hit = 0           # chop-rejection (directional hit on TRENDING)
        chop_day_falsetrend = 0               # Oct-7 bars wrongly TRENDING
        chop_day_total = 0
        recovered = demotion_total = 0        # Oct-9 healthy-trend demotions recovered
        per_day = defaultdict(lambda: {"trend": 0, "neutral": 0, "chop": 0})

        for day in days:
            s, br = data[day]
            for inst in ("NIFTY", "SENSEX"):
                series = s[inst]
                blist = br.get(inst)
                for i, row in enumerate(series):
                    breadth = blist[i] if (blist and i < len(blist)) else None
                    bucket, direction, score = classify(row, cfg, breadth)
                    _bk = {"TRENDING": "trend", "NEUTRAL": "neutral", "CHOPPY": "chop"}[bucket]
                    per_day[day][_bk] += 1
                    fwd = fwd_move(series, i)
                    if fwd is None:
                        continue
                    # trend-capture: strong directional follow-through bars
                    if abs(fwd) >= 1.0:
                        strong_total += 1
                        want = 1 if fwd > 0 else -1
                        got = 1 if direction == "BULL" else (-1 if direction == "BEAR" else 0)
                        if bucket == "TRENDING" and got == want:
                            strong_captured += 1
                    # chop-rejection: of TRENDING calls, did they follow through
                    if bucket == "TRENDING":
                        trend_total += 1
                        want = 1 if direction == "BULL" else -1
                        if (fwd > 0 and want > 0) or (fwd < 0 and want < 0):
                            trend_hit += 1
                    if day == "2026-10-07":
                        chop_day_total += 1
                        if bucket == "TRENDING":
                            chop_day_falsetrend += 1
                    if day == "2026-10-09" and i in live_demotions.get(inst, ()):
                        demotion_total += 1
                        if bucket == "TRENDING":
                            recovered += 1

        cap = strong_captured / strong_total * 100 if strong_total else 0
        rej = trend_hit / trend_total * 100 if trend_total else 0
        chop_rate = chop_day_falsetrend / chop_day_total * 100 if chop_day_total else 0
        print(f"\n{name}")
        print(f"  TREND-CAPTURE  (strong moves labelled TRENDING-correct): {strong_captured}/{strong_total} = {cap:.1f}%")
        print(f"  CHOP-REJECTION (TRENDING calls that followed through)   : {trend_hit}/{trend_total} = {rej:.1f}%")
        print(f"  Oct-7 chop-day bars wrongly TRENDING                    : {chop_day_falsetrend}/{chop_day_total} = {chop_rate:.1f}%")
        rec_rate = recovered / demotion_total * 100 if demotion_total else 0
        print(f"  Oct-9 healthy-trend demotions RECOVERED                 : {recovered}/{demotion_total} = {rec_rate:.1f}%")
        for day in days:
            pd = per_day[day]
            tot = pd["trend"] + pd["neutral"] + pd["chop"]
            print(f"    {day} [{DAY_TYPE.get(day,''):22}] TREND {pd['trend']:3} NEUTRAL {pd['neutral']:3} CHOPPY {pd['chop']:3}  (n={tot})")


if __name__ == "__main__":
    main()
