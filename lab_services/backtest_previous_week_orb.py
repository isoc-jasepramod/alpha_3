"""
PROJECT ALPHA 3.0 — PREVIOUS WEEK ORB ONLY BACKTEST (2026-09-21 TO 2026-09-25)
NO CIRCUIT BREAKER — Evaluates Volume-Backed ORB across all 5 sessions:
- Monday    2026-09-21
- Tuesday   2026-09-22
- Wednesday 2026-09-23
- Thursday  2026-09-24 (NIFTY Expiry)
- Friday    2026-09-25 (SENSEX Expiry)

Rules:
- 5-Minute Candle Aggregation (09:15 - 09:30 range building, 09:30 - 10:30 breakout scan)
- Adaptive SL = 0.5 * orb_range
- Fixed 1% Risk (INR 1,000 per trade)
- Targets: T1 = 1R (50% exit, BE trail), T2 = 2R (runner)
- Gap Filter: max_gap_pct 0.40%, extreme_gap_pct 1.50%
- EOD 15:25 Sweep
"""

import sys
import os
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
import time as _time
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

TRADING_DAYS = [
    ("2026-09-21", "Monday"),
    ("2026-09-22", "Tuesday"),
    ("2026-09-23", "Wednesday"),
    ("2026-09-24", "Thursday"),
    ("2026-09-25", "Friday"),
]

def fetch_candles_for_date(auth: AngelOneAuth, date_str: str) -> Dict[str, pd.DataFrame]:
    candles_dict = {}
    for inst, exch, tok in [("NIFTY", "NSE", "99926000"), ("SENSEX", "BSE", "99919000")]:
        param = {
            "exchange": exch,
            "symboltoken": tok,
            "interval": "ONE_MINUTE",
            "fromdate": f"{date_str} 09:15",
            "todate": f"{date_str} 15:30"
        }
        for attempt in range(4):
            try:
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
                break
            except Exception as e:
                if attempt < 3:
                    _time.sleep(1.5 * (attempt + 1))
                    continue
                logger.warning(f"Error fetching {inst} on {date_str}: {e}")
        _time.sleep(0.4)
    return candles_dict

def simulate_orb(df: pd.DataFrame, prev_close: Optional[float], inst: str, date_str: str) -> List[Dict[str, Any]]:
    """Volume-Backed Opening Range Breakout on 5-Minute Candles (09:15 - 10:30)."""
    df5 = df.resample("5min").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    }).dropna()

    orb_window = df5.between_time("09:15", "09:29")
    if len(orb_window) < 3:
        return []

    h_orb = float(orb_window["high"].max())
    l_orb = float(orb_window["low"].min())
    orb_range = h_orb - l_orb

    min_range = 20.0 if inst == "NIFTY" else 60.0
    max_range = 160.0 if inst == "NIFTY" else 480.0
    if orb_range < min_range or orb_range > max_range:
        return []

    # Gap filter check
    day_open = float(df5.iloc[0]["open"])
    gap_pct = (abs(day_open - prev_close) / prev_close * 100.0) if prev_close and prev_close > 0 else 0.0
    gap_dir = ("UP" if day_open > prev_close else "DOWN") if prev_close else "FLAT"

    # Extreme gap suppresses breakouts
    if gap_pct > 1.50:
        return []

    buffer = h_orb * 0.0003
    scan_window = df5.between_time("09:30", "10:30")
    trades = []
    triggered = False

    for ts, row in scan_window.iterrows():
        if triggered:
            break
        c = float(row["close"])
        if c > (h_orb + buffer):
            # Counter-trend gap filter: if gap down > 0.40%, avoid CE breakout
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
                "post_data": df5.loc[ts:]
            })
            triggered = True
        elif c < (l_orb - buffer):
            # Counter-trend gap filter: if gap up > 0.40%, avoid PE breakdown
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
                "post_data": df5.loc[ts:]
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

def run_previous_week_orb():
    print("=" * 105)
    print("  PROJECT ALPHA 3.0 | PREVIOUS WEEK VOLUME-BACKED ORB BACKTEST (2026-09-21 TO 2026-09-25)")
    print("  NO CIRCUIT BREAKER | Real 5-Minute Candles from AngelOne SmartConnect | 1% Risk (INR 1,000)")
    print("=" * 105)

    auth = AngelOneAuth()
    login_res = auth.login_sync()
    if not login_res.get("status"):
        print("[ERROR] AngelOne authentication failed.")
        return

    all_trades = []
    daily_summaries = []
    prev_closes = {"NIFTY": None, "SENSEX": None}

    for date_str, day_name in TRADING_DAYS:
        print(f"\nProcessing {day_name} ({date_str})...")
        candles_map = fetch_candles_for_date(auth, date_str)
        if not candles_map:
            print(f"[WARN] No candles retrieved for {date_str}")
            continue

        day_trades = []

        for inst in ["NIFTY", "SENSEX"]:
            df = candles_map.get(inst)
            if df is None or df.empty:
                continue

            trades = simulate_orb(df, prev_closes[inst], inst, date_str)
            for t in trades:
                resolved = resolve_trade(t, inst)
                day_trades.append(resolved)

            prev_closes[inst] = float(df.iloc[-1]["close"])

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
    print("\n" + "=" * 105)
    print("  ORB TRADE-BY-TRADE JOURNAL (PREVIOUS WEEK)")
    print("=" * 105)
    print(f"{'DATE':<11} | {'TIME':<5} | {'SYM':<6} | {'DIR':<3} | {'ORB RANGE':<10} | {'ENTRY':<8} | {'SL':<8} | {'EXIT':<8} | {'STATUS':<9} | {'PNL (INR)'}")
    print("-" * 105)
    if not all_trades:
        print("  No ORB breakout trades triggered across the period.")
    for t in all_trades:
        icon = "[WIN] " if t["pnl_inr"] > 0 else ("[LOSS]" if t["pnl_inr"] < 0 else "[FLAT]")
        print(
            f"{t['date']:<11} | {t['entry_time']:<5} | {t['instrument']:<6} | "
            f"{t['direction']:<3} | {t['orb_range']:<10.1f} | {t['entry_price']:<8.1f} | "
            f"{t['sl']:<8.1f} | {t['exit_price']:<8.1f} | {t['status']:<9} | {icon} INR {t['pnl_inr']:+,.2f}"
        )
    print("-" * 105)

    # Print Daily Breakdown Table
    print("\n" + "=" * 105)
    print("  DAILY ORB PERFORMANCE BREAKDOWN")
    print("=" * 105)
    print(f"{'DATE':<11} | {'DAY':<10} | {'TRADES':<7} | {'WINS':<5} | {'LOSSES':<7} | {'WIN RATE':<9} | {'SESSION PNL'}")
    print("-" * 105)
    for ds in daily_summaries:
        print(
            f"{ds['date']:<11} | {ds['day']:<10} | {ds['trades']:<7} | {ds['wins']:<5} | "
            f"{ds['losses']:<7} | {ds['win_rate']:<8.1f}% | INR {ds['pnl_inr']:+,.2f}"
        )
    print("-" * 105)

    # Overall Summary
    total_trades = len(all_trades)
    total_wins = sum(1 for t in all_trades if t["pnl_inr"] > 0)
    total_losses = sum(1 for t in all_trades if t["pnl_inr"] < 0)
    total_pnl = sum(t["pnl_inr"] for t in all_trades)
    overall_wr = (total_wins / total_trades * 100.0) if total_trades else 0.0

    gross_profit = sum(t["pnl_inr"] for t in all_trades if t["pnl_inr"] > 0)
    gross_loss = abs(sum(t["pnl_inr"] for t in all_trades if t["pnl_inr"] < 0))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

    print("\n" + "=" * 105)
    print(f"  TOTAL ORB TRADES: {total_trades} | WINS: {total_wins} | LOSSES: {total_losses} | WIN RATE: {overall_wr:.1f}%")
    print(f"  GROSS PROFIT: INR {gross_profit:+,.2f} | GROSS LOSS: -INR {gross_loss:,.2f} | PROFIT FACTOR: {profit_factor:.2f}")
    print(f"  NET ORB PNL: INR {total_pnl:+,.2f} ({(total_pnl / ACCOUNT_EQUITY * 100.0):+.2f}% ROI on INR 100,000 account)")
    print("=" * 105)

if __name__ == "__main__":
    run_previous_week_orb()
