"""
REGIME v1-vs-v2 LIVE A/B REPORT  (reads data/regime_compare/date=YYYY-MM-DD/<INST>.jsonl)
=========================================================================================
Summarizes a live session's recorded regime comparison:
  - time in each regime (v1 vs v2), and v2's HEALTHY/WEAKENING split
  - agreement on TRENDING-vs-not
  - regime transition counts (stability)
  - breadth coverage/validity and how often v2's participation_adj moved the score
  - DIVERGENCE log: candles where v1 said TRENDING but v2 said CHOPPY/NEUTRAL (or vice versa),
    with the breadth + DI context, so we can eyeball whether v2's extra caution was warranted.

Usage:
  python lab_services/report_regime_compare.py 2026-10-09
  python lab_services/report_regime_compare.py 2026-10-09 --divergence   # full divergence dump
"""
import os, sys, json
from collections import defaultdict

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data", "regime_compare")


def load(day, inst):
    p = os.path.join(BASE, f"date={day}", f"{inst}.jsonl")
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def bucket(regime):
    r = regime or "NA"
    return "TRENDING" if "TRENDING" in r else r


def report(day, inst, show_div):
    rows = load(day, inst)
    if not rows:
        print(f"\n### {inst}: no data")
        return
    n = len(rows)
    v1b = defaultdict(int); v2b = defaultdict(int); v2health = defaultdict(int)
    agree = 0; v1t = v2t = 0
    prev1 = prev2 = None
    breadth_valid = 0; part_moved = 0
    divergences = []

    for r in rows:
        v1 = r.get("v1", {}); v2 = r.get("v2", {}); br = r.get("breadth") or {}
        b1 = bucket(v1.get("regime")); b2 = bucket(v2.get("regime"))
        v1b[b1] += 1; v2b[b2] += 1
        if v2.get("health") in ("HEALTHY", "WEAKENING"):
            v2health[v2["health"]] += 1
        if (b1 == "TRENDING") == (b2 == "TRENDING"):
            agree += 1
        if prev1 is not None and v1.get("regime") != prev1: v1t += 1
        if prev2 is not None and v2.get("regime") != prev2: v2t += 1
        prev1, prev2 = v1.get("regime"), v2.get("regime")
        if br.get("valid"):
            breadth_valid += 1
        if abs(float(v2.get("participation_adj") or 0.0)) >= 1.0:
            part_moved += 1
        # divergence: v1 trending but v2 not (v2 more cautious), or opposite
        if (b1 == "TRENDING") != (b2 == "TRENDING"):
            divergences.append(r)

    print(f"\n### {inst}  ({n} candles)")
    print(f"  v1 regimes: " + ", ".join(f"{k} {v}" for k, v in sorted(v1b.items(), key=lambda x: -x[1])))
    print(f"  v2 regimes: " + ", ".join(f"{k} {v}" for k, v in sorted(v2b.items(), key=lambda x: -x[1])))
    if v2health:
        print(f"  v2 health : " + ", ".join(f"{k} {v}" for k, v in v2health.items()))
    print(f"  trending-agreement: {agree}/{n} = {agree/n*100:.0f}%")
    print(f"  transitions: v1={v1t}  v2={v2t}")
    print(f"  breadth valid: {breadth_valid}/{n} = {breadth_valid/n*100:.0f}%  "
          f"| participation moved score on {part_moved} candles")
    print(f"  divergences (v1 vs v2 on trending): {len(divergences)}")

    # show v1-TRENDING / v2-not (v2 vetoed a trend) and the reverse
    v1_only = [r for r in divergences if bucket(r['v1'].get('regime')) == 'TRENDING']
    v2_only = [r for r in divergences if bucket(r['v2'].get('regime')) == 'TRENDING']
    print(f"    v1 TRENDING but v2 NOT: {len(v1_only)}   |   v2 TRENDING but v1 NOT: {len(v2_only)}")

    if show_div:
        print(f"    --- divergence detail ---")
        for r in divergences:
            t = r.get("ts_ist", "")[11:19]
            v1 = r["v1"]; v2 = r["v2"]; br = r.get("breadth") or {}
            print(f"    {t} v1={v1.get('regime'):14} v2={v2.get('regime'):14} "
                  f"v2.dir={v2.get('direction'):7} DI+/-={v2.get('plus_di')}/{v2.get('minus_di')} "
                  f"breadth={br.get('breadth_pct')} wp={br.get('weighted_pressure_pct')}")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show_div = "--divergence" in sys.argv
    day = args[0] if args else None
    if not day:
        print("usage: report_regime_compare.py YYYY-MM-DD [--divergence]")
        return
    print("=" * 74)
    print(f"REGIME v1-vs-v2 LIVE A/B  —  {day}")
    print("=" * 74)
    for inst in ("NIFTY", "SENSEX"):
        report(day, inst, show_div)
    print("\nNOTE: v2 is NON-GATING; this is observational only. Breadth is live-only.")


if __name__ == "__main__":
    main()
