"""
MACRO TREND VETO A/B BACKTEST  (SimplifiedEngine, real option-premium replay) — offline
========================================================================================
Replays recorded days through the REAL SimplifiedPriceActionEngine + REAL RiskGovernor,
TWICE: once with macro_veto_enabled=True, once False. Resolves every emitted signal on the
option's OWN recorded forward premium path (stop/target/EOD), exactly like
backtest_all_strategies.py. Reports the delta the Macro Trend Veto makes.

The veto blocks counter-trend setups when the session is >= macro_veto_pct from open/prior-close
(no PE in a super-bull, no CE in a super-bear). We measure whether that improves SimplifiedEngine's
option P&L on recorded days — especially the counter-trend losses seen on strong-trend days.

Reliable only on days where recorded strikes track spot (clean days). Theoretical P&L; no costs.

Usage:
  python lab_services/backtest_macro_veto.py 2026-10-09
  python lab_services/backtest_macro_veto.py            # all clean recent days
"""
import os, sys, glob, json, bisect, asyncio
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyarrow.parquet as pq
from backend.strategies.simplified_engine import SimplifiedPriceActionEngine
from backend.risk.risk_governor import RiskGovernor
from backend.strategies.indicators import CandleAggregator

LAKE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "lake")
SPOT = {"26000": "NIFTY", "99926000": "NIFTY", "99919000": "SENSEX"}
STEP = {"NIFTY": 50.0, "SENSEX": 100.0}
DEFAULT_DAYS = ["2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]


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


async def run_variant(day, meta, rows, warm, veto_on):
    strat = SimplifiedPriceActionEngine({"macro_veto_enabled": veto_on})
    gov = RiskGovernor()
    gov.expiry_gate_enabled = False  # hermetic: don't let wall-clock expiry gate drop replayed signals

    # warm from the day's own early candles (as live server seeds from prior-session history)
    for inst in ("NIFTY", "SENSEX"):
        cands = warm.get(inst, [])
        try:
            strat.seed_from_candles(inst, cands)
        except Exception:
            pass

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
            gb = gov_bar[inst].on_tick(t, ltp, volume=1.0)
            if gb:
                gov.update_spot_bar(inst, gb["high"], gb["low"], gb["close"])
            smeta = {"is_spot": True, "name": inst}
            stick = {"token": tok, "ltp": ltp, "volume": float(r.get("volume", 0.0) or 0.0),
                     "exchange_timestamp": r["exchange_timestamp"]}
            await strat.on_tick(stick, smeta)
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
        cand = await strat.on_tick(otick, ometa)
        if cand:
            fin = gov.evaluate_signal(cand)
            if fin:
                signals.append({"token": tok, "dir": cand["direction"], "ets": t,
                                "entry": float(fin["entry_price"]), "stop": float(fin["stop_loss"]),
                                "target": float(fin["target"]), "qty": int(fin.get("quantity", 1))})

    # resolve on option forward premium path
    tl_ts = {k: [x[0] for x in v] for k, v in price_tl.items()}
    tl_px = {k: [x[1] for x in v] for k, v in price_tl.items()}
    for s in signals:
        a = tl_ts.get(s["token"], []); px = tl_px.get(s["token"], [])
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
        s["pnl"] = (ex - s["entry"]) * s["qty"]
    return signals


def summarize(signals):
    n = len(signals)
    if n == 0:
        return dict(n=0, pnl=0.0, w=0, l=0, ce=0, pe=0, ce_pnl=0.0, pe_pnl=0.0)
    w = sum(1 for s in signals if s["out"] == "TARGET")
    l = sum(1 for s in signals if s["out"] == "STOP")
    ce = [s for s in signals if s["dir"] == "CE"]
    pe = [s for s in signals if s["dir"] == "PE"]
    return dict(n=n, pnl=sum(s["pnl"] for s in signals), w=w, l=l,
                ce=len(ce), pe=len(pe),
                ce_pnl=sum(s["pnl"] for s in ce), pe_pnl=sum(s["pnl"] for s in pe))


async def main():
    days = [sys.argv[1]] if len(sys.argv) > 1 else DEFAULT_DAYS
    print("=" * 78)
    print("MACRO TREND VETO A/B  —  SimplifiedEngine, real option-premium replay")
    print("=" * 78)
    grand = {"on": [], "off": []}
    for day in days:
        meta = load_meta(day); rows = load_rows(day)
        if not meta or not rows:
            print(f"\n### {day}: no data")
            continue
        warm = warm_candles(rows)
        off = await run_variant(day, meta, rows, warm, veto_on=False)
        on = await run_variant(day, meta, rows, warm, veto_on=True)
        grand["off"] += off; grand["on"] += on
        so, sn = summarize(off), summarize(on)
        print(f"\n### {day}")
        print(f"  VETO OFF: n={so['n']:>2}  W/L={so['w']}/{so['l']}  "
              f"CE {so['ce']}(Rs{so['ce_pnl']:+,.0f})  PE {so['pe']}(Rs{so['pe_pnl']:+,.0f})  "
              f"=> P&L Rs{so['pnl']:+,.0f}")
        print(f"  VETO ON : n={sn['n']:>2}  W/L={sn['w']}/{sn['l']}  "
              f"CE {sn['ce']}(Rs{sn['ce_pnl']:+,.0f})  PE {sn['pe']}(Rs{sn['pe_pnl']:+,.0f})  "
              f"=> P&L Rs{sn['pnl']:+,.0f}")
        print(f"  DELTA   : {sn['n']-so['n']:+d} signals,  P&L Rs{sn['pnl']-so['pnl']:+,.0f}")

    go, gn = summarize(grand["off"]), summarize(grand["on"])
    print("\n" + "=" * 78)
    print("AGGREGATE")
    print(f"  VETO OFF: n={go['n']}  W/L={go['w']}/{go['l']}  P&L Rs{go['pnl']:+,.0f}")
    print(f"  VETO ON : n={gn['n']}  W/L={gn['w']}/{gn['l']}  P&L Rs{gn['pnl']:+,.0f}")
    print(f"  NET EFFECT OF VETO: {gn['n']-go['n']:+d} signals,  P&L Rs{gn['pnl']-go['pnl']:+,.0f}")
    print("=" * 78)
    print("Theoretical, no costs. Reliable only where recorded strikes track spot.")


if __name__ == "__main__":
    asyncio.run(main())
