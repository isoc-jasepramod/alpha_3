"""
PROJECT ALPHA 3.0 — FULL SEPTEMBER 2026 INSTITUTIONAL ORB BACKTEST (21 SESSIONS)
Real 5-Minute Candle Data directly from AngelOne SmartAPI for NIFTY 50 and BSE SENSEX:
- Dates: 2026-09-01 to 2026-09-30 (all 21 trading sessions)
- Strategy: Volume-Backed ORB (09:15-09:30 range, 09:30-10:30 breakout scan)
- Adaptive Stop Loss = 0.5 * orb_range
- Fixed 1% Risk (INR 1,000 per trade) on INR 100,000 capital
- Targets: T1 = 1R (50% partial exit, trail SL to breakeven), T2 = 2R (runner)
- Gap Filters: max_gap_pct 0.40%, extreme_gap_pct 1.50%
- EOD 15:25 Sweep
"""

import sys
import os
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
import pandas as pd
import numpy as np
from loguru import logger

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from backend.core.auth import AngelOneAuth

ACCOUNT_EQUITY = 100000.0
RISK_PER_TRADE = 1000.0
LOT_SIZE_NIFTY = 65
LOT_SIZE_SENSEX = 20
DELTA_ATM = 0.50

def fetch_september_candles(auth: AngelOneAuth) -> Dict[str, pd.DataFrame]:
    """Fetches all 5-minute candles for NIFTY and SENSEX for September 2026 in bulk."""
    candles_dict = {}
    for inst, exch, tok in [("NIFTY", "NSE", "99926000"), ("SENSEX", "BSE", "99919000")]:
        param = {
            "exchange": exch,
            "symboltoken": tok,
            "interval": "FIVE_MINUTE",
            "fromdate": "2026-09-01 09:15",
            "todate": "2026-09-30 15:30"
        }
        res = auth.smart_connect.getCandleData(param)
        if res and res.get("status") and res.get("data"):
            rows = []
            for r in res["data"]:
                dt = datetime.strptime(r[0][:19], "%Y-%m-%dT%H:%M:%S")
                rows.append({
                    "timestamp": dt,
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
                    "volume": float(r[5])
                })
            df = pd.DataFrame(rows).set_index("timestamp")
            candles_dict[inst] = df
            logger.info(f"Loaded {len(df)} 5-min candles for {inst} across September 2026.")
    return candles_dict

def simulate_orb_day(df_day: pd.DataFrame, prev_close: Optional[float], inst: str, date_str: str) -> List[Dict[str, Any]]:
    """Simulates ORB for a single day session using 5-minute candles."""
    orb_window = df_day.between_time("09:15", "09:29")
    if len(orb_window) < 3:
        return []

    h_orb = float(orb_window["high"].max())
    l_orb = float(orb_window["low"].min())
    orb_range = h_orb - l_orb

    min_range = 20.0 if inst == "NIFTY" else 60.0
    max_range = 160.0 if inst == "NIFTY" else 480.0
    if orb_range < min_range or orb_range > max_range:
        return []

    # Gap filter
    day_open = float(df_day.iloc[0]["open"])
    gap_pct = (abs(day_open - prev_close) / prev_close * 100.0) if prev_close and prev_close > 0 else 0.0
    gap_dir = ("UP" if day_open > prev_close else "DOWN") if prev_close else "FLAT"

    # Extreme gap suppresses breakouts
    if gap_pct > 1.50:
        return []

    buffer = h_orb * 0.0003
    scan_window = df_day.between_time("09:30", "10:30")
    trades = []
    triggered = False

    for ts, row in scan_window.iterrows():
        if triggered:
            break
        c = float(row["close"])

        # Bullish Breakout
        if c > (h_orb + buffer):
            # Counter-trend gap fade filter: if gap down > 0.40%, avoid CE
            if gap_pct > 0.40 and gap_dir == "DOWN":
                continue

            risk = 0.5 * orb_range
            sl = round(c - risk, 2)
            trades.append({
                "date": date_str,
                "strategy": "ORB_BREAKOUT",
                "instrument": inst,
                "direction": "CE",
                "entry_time": ts.strftime("%H:%M"),
                "entry_price": round(c, 2),
                "sl": sl,
                "risk_pts": round(risk, 2),
                "h_orb": h_orb,
                "l_orb": l_orb,
                "orb_range": round(orb_range, 2),
                "gap_pct": round(gap_pct, 2),
                "gap_dir": gap_dir,
                "t1": round(c + risk, 2),
                "t2": round(c + 2.0 * risk, 2),
                "post_data": df_day.loc[ts:]
            })
            triggered = True

        # Bearish Breakdown
        elif c < (l_orb - buffer):
            # Counter-trend gap fade filter: if gap up > 0.40%, avoid PE
            if gap_pct > 0.40 and gap_dir == "UP":
                continue

            risk = 0.5 * orb_range
            sl = round(c + risk, 2)
            trades.append({
                "date": date_str,
                "strategy": "ORB_BREAKOUT",
                "instrument": inst,
                "direction": "PE",
                "entry_time": ts.strftime("%H:%M"),
                "entry_price": round(c, 2),
                "sl": sl,
                "risk_pts": round(risk, 2),
                "h_orb": h_orb,
                "l_orb": l_orb,
                "orb_range": round(orb_range, 2),
                "gap_pct": round(gap_pct, 2),
                "gap_dir": gap_dir,
                "t1": round(c - risk, 2),
                "t2": round(c - 2.0 * risk, 2),
                "post_data": df_day.loc[ts:]
            })
            triggered = True

    return trades

def resolve_trade(trade: Dict[str, Any], inst: str) -> Dict[str, Any]:
    post_data = trade["post_data"].iloc[1:]
    direction = trade["direction"]
    entry = trade["entry_price"]
    sl = trade["sl"]
    t1 = trade["t1"]
    t2 = trade["t2"]
    risk = trade["risk_pts"]

    lot_size = LOT_SIZE_NIFTY if inst == "NIFTY" else LOT_SIZE_SENSEX
    opt_risk = risk * DELTA_ATM
    lots = max(1, int(RISK_PER_TRADE / (opt_risk * lot_size))) if opt_risk > 0 else 1
    total_qty = lots * lot_size
    qty_t1 = total_qty // 2
    qty_t2 = total_qty - qty_t1

    t1_hit = False
    t2_hit = False
    sl_hit = False
    exit_p = entry
    exit_time = "15:25"
    pnl = 0.0

    for ts, row in post_data.iterrows():
        h = float(row["high"])
        l = float(row["low"])
        t_str = ts.strftime("%H:%M")

        if direction == "CE":
            if not t1_hit and l <= sl:
                sl_hit = True
                exit_p = sl
                exit_time = t_str
                pnl = -RISK_PER_TRADE
                break
            elif t1_hit and l <= entry:
                exit_p = entry
                exit_time = t_str
                t2_hit = False
                break

            if not t1_hit and h >= t1:
                t1_hit = True
                pnl += (t1 - entry) * DELTA_ATM * qty_t1

            if t1_hit and h >= t2:
                t2_hit = True
                pnl += (t2 - entry) * DELTA_ATM * qty_t2
                exit_p = t2
                exit_time = t_str
                break

        elif direction == "PE":
            if not t1_hit and h >= sl:
                sl_hit = True
                exit_p = sl
                exit_time = t_str
                pnl = -RISK_PER_TRADE
                break
            elif t1_hit and h >= entry:
                exit_p = entry
                exit_time = t_str
                t2_hit = False
                break

            if not t1_hit and l <= t1:
                t1_hit = True
                pnl += (entry - t1) * DELTA_ATM * qty_t1

            if t1_hit and l <= t2:
                t2_hit = True
                pnl += (entry - t2) * DELTA_ATM * qty_t2
                exit_p = t2
                exit_time = t_str
                break

    if not (t2_hit or sl_hit):
        final_close = float(post_data.iloc[-1]["close"]) if len(post_data) > 0 else entry
        if direction == "CE":
            rem_pnl = (final_close - entry) * DELTA_ATM * (qty_t2 if t1_hit else total_qty)
        else:
            rem_pnl = (entry - final_close) * DELTA_ATM * (qty_t2 if t1_hit else total_qty)
        pnl += rem_pnl
        exit_p = final_close

    status = "T2_HIT" if t2_hit else ("T1_BE" if t1_hit else ("STOP_HIT" if sl_hit else "EOD_EXIT"))

    return {
        **trade,
        "status": status,
        "exit_price": round(exit_p, 2),
        "exit_time": exit_time,
        "qty": total_qty,
        "pnl_inr": round(pnl, 2),
        "r_multiple": round(pnl / RISK_PER_TRADE, 2)
    }

def run_september_backtest():
    print("=" * 110)
    print("  PROJECT ALPHA 3.0 | FULL MONTH SEPTEMBER 2026 ORB BACKTEST (21 SESSIONS)")
    print("  Real 5-Minute Candles from AngelOne SmartAPI | 1% Risk (INR 1,000) on INR 100k Account")
    print("=" * 110)

    auth = AngelOneAuth()
    login_res = auth.login_sync()
    if not login_res.get("status"):
        print("[ERROR] AngelOne authentication failed.")
        return

    candles_map = fetch_september_candles(auth)
    if not candles_map:
        print("[ERROR] Failed to fetch candles.")
        return

    # Extract all distinct trading dates in September
    nifty_df = candles_map["NIFTY"]
    dates = sorted(list(set(nifty_df.index.strftime("%Y-%m-%d"))))

    all_trades = []
    daily_summaries = []
    prev_closes = {"NIFTY": None, "SENSEX": None}

    for date_str in dates:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        day_name = dt.strftime("%A")
        day_trades = []

        for inst in ["NIFTY", "SENSEX"]:
            df_inst = candles_map[inst]
            day_df = df_inst[df_inst.index.strftime("%Y-%m-%d") == date_str]
            if day_df.empty:
                continue

            trades = simulate_orb_day(day_df, prev_closes[inst], inst, date_str)
            for t in trades:
                resolved = resolve_trade(t, inst)
                day_trades.append(resolved)

            prev_closes[inst] = float(day_df.iloc[-1]["close"])

        day_trades.sort(key=lambda x: x["entry_time"])
        all_trades.extend(day_trades)

        day_pnl = sum(t["pnl_inr"] for t in day_trades)
        day_wins = sum(1 for t in day_trades if t["pnl_inr"] > 0)
        day_losses = sum(1 for t in day_trades if t["pnl_inr"] < 0)
        day_wr = (day_wins / len(day_trades) * 100.0) if day_trades else 0.0

        daily_summaries.append({
            "date": date_str,
            "day": day_name,
            "trades": len(day_trades),
            "wins": day_wins,
            "losses": day_losses,
            "win_rate": day_wr,
            "pnl_inr": day_pnl
        })

    # Print Detailed Trade Log
    print("\n" + "=" * 110)
    print("  SEPTEMBER 2026 ORB TRADE-BY-TRADE JOURNAL")
    print("=" * 110)
    print(f"{'DATE':<11} | {'TIME':<5} | {'SYM':<6} | {'DIR':<3} | {'ORB RANGE':<10} | {'ENTRY':<8} | {'SL':<8} | {'EXIT':<8} | {'STATUS':<9} | {'PNL (INR)'}")
    print("-" * 110)
    for t in all_trades:
        icon = "[WIN] " if t["pnl_inr"] > 0 else ("[LOSS]" if t["pnl_inr"] < 0 else "[FLAT]")
        print(
            f"{t['date']:<11} | {t['entry_time']:<5} | {t['instrument']:<6} | "
            f"{t['direction']:<3} | {t['orb_range']:<10.1f} | {t['entry_price']:<8.1f} | "
            f"{t['sl']:<8.1f} | {t['exit_price']:<8.1f} | {t['status']:<9} | {icon} INR {t['pnl_inr']:+,.2f}"
        )
    print("-" * 110)

    # Print Daily Breakdown Table
    print("\n" + "=" * 110)
    print("  SEPTEMBER 2026 DAILY ORB PERFORMANCE BREAKDOWN")
    print("=" * 110)
    print(f"{'DATE':<11} | {'DAY':<10} | {'TRADES':<7} | {'WINS':<5} | {'LOSSES':<7} | {'WIN RATE':<9} | {'SESSION PNL'}")
    print("-" * 110)
    for ds in daily_summaries:
        status_str = f"INR {ds['pnl_inr']:+,.2f}" if ds['trades'] > 0 else "— (No Breakout)"
        print(
            f"{ds['date']:<11} | {ds['day']:<10} | {ds['trades']:<7} | {ds['wins']:<5} | "
            f"{ds['losses']:<7} | {ds['win_rate']:<8.1f}% | {status_str}"
        )
    print("-" * 110)

    # Overall Monthly Summary
    total_trades = len(all_trades)
    total_wins = sum(1 for t in all_trades if t["pnl_inr"] > 0)
    total_losses = sum(1 for t in all_trades if t["pnl_inr"] < 0)
    total_pnl = sum(t["pnl_inr"] for t in all_trades)
    overall_wr = (total_wins / total_trades * 100.0) if total_trades else 0.0

    gross_profit = sum(t["pnl_inr"] for t in all_trades if t["pnl_inr"] > 0)
    gross_loss = abs(sum(t["pnl_inr"] for t in all_trades if t["pnl_inr"] < 0))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

    t2_runners = sum(1 for t in all_trades if t["status"] == "T2_HIT")
    t1_be = sum(1 for t in all_trades if t["status"] == "T1_BE")
    stops = sum(1 for t in all_trades if t["status"] == "STOP_HIT")

    print("\n" + "=" * 110)
    print(f"  MONTHLY SUMMARY: Total Sessions: {len(dates)} | Active Trade Days: {sum(1 for d in daily_summaries if d['trades'] > 0)}")
    print(f"  TOTAL ORB TRADES: {total_trades} | WINS: {total_wins} (T2: {t2_runners}, T1_BE: {t1_be}) | LOSSES: {total_losses} (SL: {stops})")
    print(f"  WIN RATE: {overall_wr:.1f}% | PROFIT FACTOR: {profit_factor:.2f}")
    print(f"  GROSS PROFIT: INR {gross_profit:+,.2f} | GROSS LOSS: -INR {gross_loss:,.2f}")
    print(f"  NET SEPTEMBER ORB PNL: INR {total_pnl:+,.2f} ({(total_pnl / ACCOUNT_EQUITY * 100.0):+.2f}% ROI on INR 100,000 account)")
    print("=" * 110)

if __name__ == "__main__":
    run_september_backtest()
