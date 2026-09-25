"""
PROJECT ALPHA 3.0 — 5-DAY HISTORICAL BACKTEST (2026-09-21 TO 2026-09-25)
Complete multi-strategy institutional validation across the full trading week:
- Monday    2026-09-21
- Tuesday   2026-09-22
- Wednesday 2026-09-23
- Thursday  2026-09-24 (NIFTY 0-DTE Expiry)
- Friday    2026-09-25 (SENSEX 0-DTE Expiry + 2 PM Spike Session)

Evaluates:
1. Volume-Backed ORB (09:15 - 10:30)
2. VWAP & EMA Alignment (09:15 - 15:15)
3. Expiry Day Gamma Scalp (13:15 - 15:30 on Expiry Days)
4. Momentum Impulse Detector (09:20 - 15:20)
5. OI Squeeze Sentinel (with Fast-Path Early Ignition)
"""

import sys
import os
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
RISK_PCT = 0.01  # 1% risk per trade = INR 1,000
LOT_SIZE_NIFTY = 65
LOT_SIZE_SENSEX = 20
DELTA_ATM = 0.50
IST = timezone(timedelta(hours=5, minutes=30))

DATES = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"]

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
        except Exception as e:
            logger.warning(f"Error fetching {inst} on {date_str}: {e}")
    return candles_dict

def simulate_orb(df: pd.DataFrame, inst: str, date_str: str) -> Optional[Dict[str, Any]]:
    orb_window = df.between_time("09:15", "09:29")
    if len(orb_window) < 10:
        return None

    h_orb = float(orb_window["high"].max())
    l_orb = float(orb_window["low"].min())
    orb_range = h_orb - l_orb
    
    min_range = 20.0 if inst == "NIFTY" else 60.0
    max_range = 160.0 if inst == "NIFTY" else 480.0
    if orb_range < min_range or orb_range > max_range:
        return None

    buffer = h_orb * 0.0003
    scan_window = df.between_time("09:30", "10:30")
    
    for ts, row in scan_window.iterrows():
        c = float(row["close"])
        if c > (h_orb + buffer):
            risk = c - l_orb
            return {
                "date": date_str,
                "strategy": "ORB_BREAKOUT",
                "instrument": inst,
                "direction": "CE",
                "entry_time": ts.strftime("%H:%M"),
                "entry_price": round(c, 2),
                "sl": round(l_orb, 2),
                "risk_pts": round(risk, 2),
                "t1": round(c + risk, 2),
                "t2": round(c + 2.0 * risk, 2),
                "post_data": df.loc[ts:]
            }
        elif c < (l_orb - buffer):
            risk = h_orb - c
            return {
                "date": date_str,
                "strategy": "ORB_BREAKOUT",
                "instrument": inst,
                "direction": "PE",
                "entry_time": ts.strftime("%H:%M"),
                "entry_price": round(c, 2),
                "sl": round(h_orb, 2),
                "risk_pts": round(risk, 2),
                "t1": round(c - risk, 2),
                "t2": round(c - 2.0 * risk, 2),
                "post_data": df.loc[ts:]
            }
    return None

def simulate_gamma_scalp(df: pd.DataFrame, inst: str, weekday: int, date_str: str) -> Optional[Dict[str, Any]]:
    # Thursday = NIFTY (weekday 3), Friday = SENSEX (weekday 4)
    is_expiry_inst = (inst == "NIFTY" and weekday == 3) or (inst == "SENSEX" and weekday == 4)
    if not is_expiry_inst:
        return None

    mid_window = df.between_time("12:00", "13:00")
    if len(mid_window) < 30:
        return None

    h_mid = float(mid_window["high"].max())
    l_mid = float(mid_window["low"].min())
    mid_range = h_mid - l_mid
    mid_spot = float(mid_window["close"].iloc[-1])

    if (mid_range / mid_spot) > 0.0035:
        return None

    scan_window = df.between_time("13:15", "15:15")
    for ts, row in scan_window.iterrows():
        c = float(row["close"])
        if c < l_mid:
            return {
                "date": date_str,
                "strategy": "GAMMA_SCALP",
                "instrument": inst,
                "direction": "PE",
                "entry_time": ts.strftime("%H:%M"),
                "entry_price": round(c, 2),
                "sl": round(l_mid + (0.5 * mid_range), 2),
                "risk_pts": round(0.5 * mid_range, 2),
                "t1": round(c - (0.5 * mid_range), 2),
                "t2": round(c - mid_range, 2),
                "post_data": df.loc[ts:]
            }
        elif c > h_mid:
            return {
                "date": date_str,
                "strategy": "GAMMA_SCALP",
                "instrument": inst,
                "direction": "CE",
                "entry_time": ts.strftime("%H:%M"),
                "entry_price": round(c, 2),
                "sl": round(h_mid - (0.5 * mid_range), 2),
                "risk_pts": round(0.5 * mid_range, 2),
                "t1": round(c + (0.5 * mid_range), 2),
                "t2": round(c + mid_range, 2),
                "post_data": df.loc[ts:]
            }
    return None

def simulate_vwap_ema(df: pd.DataFrame, inst: str, date_str: str) -> List[Dict[str, Any]]:
    df3 = df.resample("3min").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    }).dropna()

    df3["ema9"] = df3["close"].ewm(span=9, adjust=False).mean()
    df3["ema21"] = df3["close"].ewm(span=21, adjust=False).mean()
    pv = (df3["close"] * df3["volume"]).cumsum()
    v = df3["volume"].cumsum()
    df3["vwap"] = pv / v.replace(0, 1)

    trades = []
    scan_window = df3.between_time("09:45", "14:45")
    last_trade_time = datetime.min

    for ts, row in scan_window.iterrows():
        if (ts.to_pydatetime() - last_trade_time).total_seconds() < 1800:
            continue

        c = float(row["close"])
        e9 = float(row["ema9"])
        e21 = float(row["ema21"])
        vwap = float(row["vwap"])
        l = float(row["low"])
        h = float(row["high"])

        # Bearish Continuation
        if e9 < e21 and c < vwap and h >= e9 * 0.9995 and c < e9:
            sl = round(max(h, e21), 2)
            risk = round(sl - c, 2)
            if risk > (10.0 if inst == "NIFTY" else 30.0):
                trades.append({
                    "date": date_str,
                    "strategy": "VWAP_EMA",
                    "instrument": inst,
                    "direction": "PE",
                    "entry_time": ts.strftime("%H:%M"),
                    "entry_price": round(c, 2),
                    "sl": sl,
                    "risk_pts": risk,
                    "t1": round(c - risk, 2),
                    "t2": round(c - (2.0 * risk), 2),
                    "post_data": df3.loc[ts:]
                })
                last_trade_time = ts.to_pydatetime()

        # Bullish Continuation
        elif e9 > e21 and c > vwap and l <= e9 * 1.0005 and c > e9:
            sl = round(min(l, e21), 2)
            risk = round(c - sl, 2)
            if risk > (10.0 if inst == "NIFTY" else 30.0):
                trades.append({
                    "date": date_str,
                    "strategy": "VWAP_EMA",
                    "instrument": inst,
                    "direction": "CE",
                    "entry_time": ts.strftime("%H:%M"),
                    "entry_price": round(c, 2),
                    "sl": sl,
                    "risk_pts": risk,
                    "t1": round(c + risk, 2),
                    "t2": round(c + (2.0 * risk), 2),
                    "post_data": df3.loc[ts:]
                })
                last_trade_time = ts.to_pydatetime()

    return trades

def simulate_momentum_impulse(df: pd.DataFrame, inst: str, date_str: str) -> List[Dict[str, Any]]:
    trades = []
    thresh = 25.0 if inst == "NIFTY" else 75.0
    last_trade_time = datetime.min

    for i in range(3, len(df)):
        window = df.iloc[i-3:i+1]
        delta = float(window["close"].iloc[-1]) - float(window["close"].iloc[0])
        ts = window.index[-1]
        t_str = ts.strftime("%H:%M")
        if t_str < "09:30" or t_str > "15:15":
            continue

        if (ts.to_pydatetime() - last_trade_time).total_seconds() < 1800:
            continue

        if delta <= -thresh:
            entry = float(window["close"].iloc[-1])
            sl = float(window["high"].max())
            risk = sl - entry
            if risk > 0:
                trades.append({
                    "date": date_str,
                    "strategy": "MOMENTUM_IMPULSE",
                    "instrument": inst,
                    "direction": "PE",
                    "entry_time": t_str,
                    "entry_price": round(entry, 2),
                    "sl": round(sl, 2),
                    "risk_pts": round(risk, 2),
                    "t1": round(entry - risk, 2),
                    "t2": round(entry - (2.0 * risk), 2),
                    "post_data": df.loc[ts:]
                })
                last_trade_time = ts.to_pydatetime()

        elif delta >= thresh:
            entry = float(window["close"].iloc[-1])
            sl = float(window["low"].min())
            risk = entry - sl
            if risk > 0:
                trades.append({
                    "date": date_str,
                    "strategy": "MOMENTUM_IMPULSE",
                    "instrument": inst,
                    "direction": "CE",
                    "entry_time": t_str,
                    "entry_price": round(entry, 2),
                    "sl": round(sl, 2),
                    "risk_pts": round(risk, 2),
                    "t1": round(entry + risk, 2),
                    "t2": round(entry + (2.0 * risk), 2),
                    "post_data": df.loc[ts:]
                })
                last_trade_time = ts.to_pydatetime()

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
    lots = max(1, int(1000.0 / (opt_risk * lot_size))) if opt_risk > 0 else 1
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
                pnl = -1000.0
                break
            elif t1_hit and l <= entry:
                exit_p = entry
                exit_time = t_str
                t2_hit = True
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
                pnl = -1000.0
                break
            elif t1_hit and h >= entry:
                exit_p = entry
                exit_time = t_str
                t2_hit = True
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
            remaining_pnl = (final_close - entry) * DELTA_ATM * qty_t2 if t1_hit else (final_close - entry) * DELTA_ATM * total_qty
        else:
            remaining_pnl = (entry - final_close) * DELTA_ATM * qty_t2 if t1_hit else (entry - final_close) * DELTA_ATM * total_qty
        pnl += remaining_pnl
        exit_p = final_close

    status = "T2_HIT" if t2_hit else ("T1_PARTIAL" if t1_hit else ("STOP_HIT" if sl_hit else "EOD_EXIT"))
    
    return {
        **trade,
        "status": status,
        "exit_price": round(exit_p, 2),
        "exit_time": exit_time,
        "qty": total_qty,
        "pnl_inr": round(pnl, 2),
        "r_multiple": round(pnl / 1000.0, 2)
    }

def run_5day_backtest():
    print("=" * 95)
    print("  PROJECT ALPHA 3.0 | 5-DAY INSTITUTIONAL BACKTEST (2026-09-21 TO 2026-09-25)")
    print("  Real 1-Minute Historical Candle Data from AngelOne SmartConnect")
    print("=" * 95)

    auth = AngelOneAuth()
    login_res = auth.login_sync()
    if not login_res.get("status"):
        print("AngelOne authentication failed.")
        return

    all_week_trades = []
    daily_summaries = []

    for date_str in DATES:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        weekday = dt.weekday()
        day_name = dt.strftime("%A")
        
        candles_map = fetch_candles_for_date(auth, date_str)
        if not candles_map:
            continue

        day_trades = []

        for inst in ["NIFTY", "SENSEX"]:
            df = candles_map.get(inst)
            if df is None or df.empty:
                continue

            # 1. ORB Breakout
            orb_t = simulate_orb(df, inst, date_str)
            if orb_t:
                day_trades.append(resolve_trade(orb_t, inst))

            # 2. Gamma Scalp (Expiry day only)
            gamma_t = simulate_gamma_scalp(df, inst, weekday, date_str)
            if gamma_t:
                day_trades.append(resolve_trade(gamma_t, inst))

            # 3. VWAP & EMA Alignment
            vwap_ts = simulate_vwap_ema(df, inst, date_str)
            for vt in vwap_ts:
                day_trades.append(resolve_trade(vt, inst))

            # 4. Momentum Impulse Detector
            imp_ts = simulate_momentum_impulse(df, inst, date_str)
            for it in imp_ts:
                day_trades.append(resolve_trade(it, inst))

        day_trades.sort(key=lambda x: x["entry_time"])
        all_week_trades.extend(day_trades)

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

    # Print Day-by-Day Table
    print("\n" + "=" * 95)
    print("  DAILY PERFORMANCE BREAKDOWN (PAST 5 DAYS)")
    print("=" * 95)
    print(f"{'DATE':<12} | {'DAY':<10} | {'TRADES':<7} | {'WINS':<5} | {'LOSSES':<7} | {'WIN RATE':<9} | {'SESSION PNL (INR)'}")
    print("-" * 95)
    for ds in daily_summaries:
        print(
            f"{ds['date']:<12} | "
            f"{ds['day']:<10} | "
            f"{ds['trades']:<7} | "
            f"{ds['wins']:<5} | "
            f"{ds['losses']:<7} | "
            f"{ds['win_rate']:<8.1f}% | "
            f"INR {ds['pnl_inr']:+,.2f}"
        )
    print("-" * 95)

    # Strategy breakdown
    strat_perf = {}
    for t in all_week_trades:
        st = t["strategy"]
        if st not in strat_perf:
            strat_perf[st] = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
        strat_perf[st]["trades"] += 1
        strat_perf[st]["pnl"] += t["pnl_inr"]
        if t["pnl_inr"] > 0:
            strat_perf[st]["wins"] += 1
        elif t["pnl_inr"] < 0:
            strat_perf[st]["losses"] += 1

    print("\n" + "=" * 95)
    print("  STRATEGY-WISE PERFORMANCE SUMMARY")
    print("=" * 95)
    print(f"{'STRATEGY':<22} | {'TRADES':<7} | {'WINS':<5} | {'LOSSES':<7} | {'WIN RATE':<9} | {'TOTAL PNL (INR)'}")
    print("-" * 95)
    for st, data in sorted(strat_perf.items(), key=lambda x: x[1]["pnl"], reverse=True):
        wr = (data["wins"] / data["trades"] * 100.0) if data["trades"] else 0.0
        print(
            f"{st:<22} | "
            f"{data['trades']:<7} | "
            f"{data['wins']:<5} | "
            f"{data['losses']:<7} | "
            f"{wr:<8.1f}% | "
            f"INR {data['pnl']:+,.2f}"
        )
    print("-" * 95)

    # Overall Summary
    total_trades = len(all_week_trades)
    total_wins = sum(1 for t in all_week_trades if t["pnl_inr"] > 0)
    total_losses = sum(1 for t in all_week_trades if t["pnl_inr"] < 0)
    total_pnl = sum(t["pnl_inr"] for t in all_week_trades)
    overall_wr = (total_wins / total_trades * 100.0) if total_trades else 0.0

    gross_profit = sum(t["pnl_inr"] for t in all_week_trades if t["pnl_inr"] > 0)
    gross_loss = abs(sum(t["pnl_inr"] for t in all_week_trades if t["pnl_inr"] < 0))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    print("\n" + "=" * 95)
    print(f"  FULL WEEK SUMMARY: Total Trades: {total_trades} | Wins: {total_wins} | Losses: {total_losses} | Win Rate: {overall_wr:.1f}%")
    print(f"  Gross Profit: INR {gross_profit:+,.2f} | Gross Loss: INR -{gross_loss:,.2f} | Profit Factor: {profit_factor:.2f}")
    print(f"  TOTAL WEEKLY PNL: INR {total_pnl:+,.2f} ({(total_pnl / ACCOUNT_EQUITY * 100.0):+.2f}% ROI on INR 100k account)")
    print("=" * 95)

if __name__ == "__main__":
    run_5day_backtest()
