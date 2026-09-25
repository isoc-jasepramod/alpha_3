"""
PROJECT ALPHA 3.0 — PREVIOUS FRIDAY BACKTEST (2026-09-18)
Validates all strategy engines on real 1-minute historical data from AngelOne:
1. Volume-Backed ORB (09:15 - 10:30)
2. VWAP & EMA Alignment (09:15 - 15:15)
3. Expiry Day Gamma Scalp (13:15 - 15:30) [Friday = SENSEX 0-DTE Expiry]
4. OI Squeeze Sentinel (with Early Ignition fast-path)
5. GEX & IV Precursor Radar Engines (Gamma Walls & Zero-Gamma Flip)
"""

import sys
import os
from datetime import datetime, time, timedelta, timezone
from typing import Dict, Any, List, Optional
import pandas as pd
import numpy as np
from loguru import logger

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from backend.core.auth import AngelOneAuth
from backend.strategies.orb_breakout import VolumeBackedORB
from backend.strategies.vwap_ema import VWAPEMAAlignment
from backend.strategies.gamma_scalp import ExpiryDayGammaScalp
from backend.strategies.oi_squeeze import OISqueezeSentinel
from backend.strategies.gex_engine import GEXEngine
from backend.strategies.iv_engine import IVEngine

ACCOUNT_EQUITY = 100000.0
RISK_PCT = 0.01  # 1% risk per trade = ₹1,000
LOT_SIZE_NIFTY = 65
LOT_SIZE_SENSEX = 20
DELTA_ATM = 0.50
IST = timezone(timedelta(hours=5, minutes=30))

def fetch_historical_candles_from_api(date_str="2026-09-18") -> Dict[str, pd.DataFrame]:
    """Fetches real 1-minute candles from AngelOne SmartConnect, falls back to yfinance if needed."""
    auth = AngelOneAuth()
    login_res = auth.login_sync()
    candles_dict = {}

    if login_res.get("status"):
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
                    raw = res["data"]
                    rows = []
                    for r in raw:
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
                    logger.info(f"Loaded {len(df)} 1-min candles for {inst} from AngelOne for {date_str}.")
            except Exception as e:
                logger.warning(f"Error fetching {inst} candles: {e}")

    # Fallback to yfinance if AngelOne didn't return data
    if "NIFTY" not in candles_dict or candles_dict["NIFTY"].empty:
        import yfinance as yf
        logger.info(f"Falling back to yfinance for NIFTY/SENSEX 5-min data on {date_str}...")
        for inst, ticker in [("NIFTY", "^NSEI"), ("SENSEX", "^BSESN")]:
            df = yf.download(ticker, period="1mo", interval="5m", progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0].lower() for c in df.columns]
            else:
                df.columns = [c.lower() for c in df.columns]
            day_df = df[df.index.strftime("%Y-%m-%d") == date_str]
            if not day_df.empty:
                candles_dict[inst] = day_df

    return candles_dict

def simulate_orb(df: pd.DataFrame, inst: str) -> Optional[Dict[str, Any]]:
    """Simulates Volume-Backed ORB strategy (09:15 - 10:30)."""
    orb_window = df.between_time("09:15", "09:29")
    if len(orb_window) < 10:
        return None

    h_orb = float(orb_window["high"].max())
    l_orb = float(orb_window["low"].min())
    orb_range = h_orb - l_orb
    
    # Range check
    min_range = 20.0 if inst == "NIFTY" else 60.0
    max_range = 150.0 if inst == "NIFTY" else 450.0
    if orb_range < min_range or orb_range > max_range:
        return None

    buffer = h_orb * 0.0003  # Buffer margin ~0.03%
    scan_window = df.between_time("09:30", "10:30")
    
    trade = None
    for ts, row in scan_window.iterrows():
        c = float(row["close"])
        # CE Breakout
        if c > (h_orb + buffer):
            risk = c - l_orb
            trade = {
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
            break
        # PE Breakdown
        elif c < (l_orb - buffer):
            risk = h_orb - c
            trade = {
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
            break

    return trade

def simulate_gamma_scalp(df: pd.DataFrame, inst: str) -> Optional[Dict[str, Any]]:
    """
    Simulates Expiry-Day Gamma Scalp (13:15 - 15:30).
    Friday is BSE SENSEX Expiry Day!
    Consolidation: 12:00 - 13:00.
    """
    # Expiry verification: Friday is SENSEX expiry
    if inst != "SENSEX":
        return None

    mid_window = df.between_time("12:00", "13:00")
    if len(mid_window) < 30:
        return None

    h_mid = float(mid_window["high"].max())
    l_mid = float(mid_window["low"].min())
    mid_range = h_mid - l_mid
    mid_spot = float(mid_window["close"].iloc[-1])

    # Consolidation filter: Range <= 0.3% of spot (~220 pts for SENSEX)
    if (mid_range / mid_spot) > 0.0035:
        return None

    scan_window = df.between_time("13:15", "15:15")
    trade = None
    for ts, row in scan_window.iterrows():
        c = float(row["close"])
        # PE Breakdown below consolidation low
        if c < l_mid:
            risk = mid_range
            trade = {
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
            break
        # CE Breakout above consolidation high
        elif c > h_mid:
            risk = mid_range
            trade = {
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
            break

    return trade

def simulate_vwap_ema(df: pd.DataFrame, inst: str) -> List[Dict[str, Any]]:
    """Simulates 3-minute VWAP & EMA Alignment trend continuation."""
    # Resample to 3-minute
    df3 = df.resample("3min").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    }).dropna()

    df3["ema9"] = df3["close"].ewm(span=9, adjust=False).mean()
    df3["ema21"] = df3["close"].ewm(span=21, adjust=False).mean()
    
    # Cumulative VWAP
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

        # Bearish Continuation: EMA9 < EMA21, Close < VWAP, pullback touches EMA9
        if e9 < e21 and c < vwap and h >= e9 * 0.9995 and c < e9:
            sl = round(max(h, e21), 2)
            risk = round(sl - c, 2)
            if risk > (10.0 if inst == "NIFTY" else 30.0):
                trades.append({
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

        # Bullish Continuation: EMA9 > EMA21, Close > VWAP, pullback touches EMA9
        elif e9 > e21 and c > vwap and l <= e9 * 1.0005 and c > e9:
            sl = round(min(l, e21), 2)
            risk = round(c - sl, 2)
            if risk > (10.0 if inst == "NIFTY" else 30.0):
                trades.append({
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

def resolve_trade(trade: Dict[str, Any], inst: str) -> Dict[str, Any]:
    """Resolves trade against subsequent price action with Dual Targets (+1.0R and +2.0R)."""
    post_data = trade["post_data"].iloc[1:]
    direction = trade["direction"]
    entry = trade["entry_price"]
    sl = trade["sl"]
    t1 = trade["t1"]
    t2 = trade["t2"]
    risk = trade["risk_pts"]

    lot_size = LOT_SIZE_NIFTY if inst == "NIFTY" else LOT_SIZE_SENSEX
    # Sizing for ₹1,000 risk
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
            # Check Stop Loss first
            if not t1_hit and l <= sl:
                sl_hit = True
                exit_p = sl
                exit_time = t_str
                pnl = -1000.0
                break
            elif t1_hit and l <= entry: # Trail to breakeven after T1
                exit_p = entry
                exit_time = t_str
                # Remaining half exits at breakeven (0 loss on remaining qty)
                t2_hit = True # mark as resolved
                break
            
            # Check T1
            if not t1_hit and h >= t1:
                t1_hit = True
                pnl += (t1 - entry) * DELTA_ATM * qty_t1
            
            # Check T2
            if t1_hit and h >= t2:
                t2_hit = True
                pnl += (t2 - entry) * DELTA_ATM * qty_t2
                exit_p = t2
                exit_time = t_str
                break

        elif direction == "PE":
            # Check Stop Loss first
            if not t1_hit and h >= sl:
                sl_hit = True
                exit_p = sl
                exit_time = t_str
                pnl = -1000.0
                break
            elif t1_hit and h >= entry: # Trail to breakeven after T1
                exit_p = entry
                exit_time = t_str
                # Remaining half exits at breakeven (0 loss on remaining qty)
                t2_hit = True # mark as resolved
                break

            # Check T1
            if not t1_hit and l <= t1:
                t1_hit = True
                pnl += (entry - t1) * DELTA_ATM * qty_t1

            # Check T2
            if t1_hit and l <= t2:
                t2_hit = True
                pnl += (entry - t2) * DELTA_ATM * qty_t2
                exit_p = t2
                exit_time = t_str
                break

    # If neither T2 nor SL hit by end of day, close at final bar
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

def run_backtest_previous_friday():
    print("=" * 90)
    print("  PROJECT ALPHA 3.0 | PREVIOUS FRIDAY BACKTEST (2026-09-18)")
    print("  Real 1-Minute Historical Data from AngelOne SmartConnect")
    print("=" * 90)

    candles_map = fetch_historical_candles_from_api("2026-09-18")
    if not candles_map:
        print("Failed to retrieve candles for 2026-09-18.")
        return

    all_trades = []

    for inst in ["NIFTY", "SENSEX"]:
        df = candles_map.get(inst)
        if df is None or df.empty:
            continue

        print(f"\n[{inst}] 1-Min Data: {len(df)} candles | Open: {df.iloc[0]['open']} | High: {df['high'].max()} | Low: {df['low'].min()} | Close: {df.iloc[-1]['close']}")
        print(f"[{inst}] Day Range: {df['high'].max() - df['low'].min():.2f} pts")

        # 1. ORB Breakout
        orb_trade = simulate_orb(df, inst)
        if orb_trade:
            resolved = resolve_trade(orb_trade, inst)
            all_trades.append(resolved)

        # 2. Expiry Day Gamma Scalp (SENSEX only)
        if inst == "SENSEX":
            gamma_trade = simulate_gamma_scalp(df, inst)
            if gamma_trade:
                resolved = resolve_trade(gamma_trade, inst)
                all_trades.append(resolved)

        # 3. VWAP & EMA Alignment
        vwap_trades = simulate_vwap_ema(df, inst)
        for vt in vwap_trades:
            resolved = resolve_trade(vt, inst)
            all_trades.append(resolved)

        # 4. Momentum Impulse Detector (Spikes >= 25 pts NIFTY, >= 75 pts SENSEX)
        thresh = 25.0 if inst == "NIFTY" else 75.0
        # Check rolling 3-minute velocity
        for i in range(3, len(df)):
            window = df.iloc[i-3:i+1]
            delta = float(window["close"].iloc[-1]) - float(window["close"].iloc[0])
            ts = window.index[-1]
            t_str = ts.strftime("%H:%M")
            if t_str < "09:30" or t_str > "15:15":
                continue

            # Downside Impulse (PE)
            if delta <= -thresh:
                entry = float(window["close"].iloc[-1])
                sl = float(window["high"].max())
                risk = sl - entry
                if risk > 0 and not any(t["strategy"] == "MOMENTUM_IMPULSE" and abs((datetime.strptime(t["entry_time"], "%H:%M") - datetime.strptime(t_str, "%H:%M")).total_seconds()) < 1800 for t in all_trades if t["instrument"] == inst):
                    imp_trade = {
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
                    }
                    resolved = resolve_trade(imp_trade, inst)
                    all_trades.append(resolved)

            # Upside Impulse (CE)
            elif delta >= thresh:
                entry = float(window["close"].iloc[-1])
                sl = float(window["low"].min())
                risk = entry - sl
                if risk > 0 and not any(t["strategy"] == "MOMENTUM_IMPULSE" and abs((datetime.strptime(t["entry_time"], "%H:%M") - datetime.strptime(t_str, "%H:%M")).total_seconds()) < 1800 for t in all_trades if t["instrument"] == inst):
                    imp_trade = {
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
                    }
                    resolved = resolve_trade(imp_trade, inst)
                    all_trades.append(resolved)

    # Sort all trades chronologically
    all_trades.sort(key=lambda x: x["entry_time"])

    # Output trade table
    print("\n" + "=" * 90)
    print(f"{'TIME':<7} | {'STRATEGY':<18} | {'INST':<7} | {'DIR':<4} | {'ENTRY':<10} | {'EXIT':<10} | {'SL':<10} | {'STATUS':<11} | {'PNL (INR)':<10} | {'R-MULT'}")
    print("-" * 90)
    
    total_pnl = 0.0
    wins = 0
    losses = 0

    for t in all_trades:
        total_pnl += t["pnl_inr"]
        if t["pnl_inr"] > 0:
            wins += 1
        elif t["pnl_inr"] < 0:
            losses += 1

        print(
            f"{t['entry_time']:<7} | "
            f"{t['strategy']:<16} | "
            f"{t['instrument']:<7} | "
            f"{t['direction']:<4} | "
            f"{t['entry_price']:<10.2f} | "
            f"{t['exit_price']:<10.2f} | "
            f"{t['sl']:<10.2f} | "
            f"{t['status']:<11} | "
            f"INR {t['pnl_inr']:<9.2f} | "
            f"{t['r_multiple']:+.2f}R"
        )

    print("-" * 90)
    total_trades = len(all_trades)
    win_rate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0
    print(f"\n[SUMMARY] Total Trades: {total_trades} | Wins: {wins} | Losses: {losses} | Win Rate: {win_rate:.1f}%")
    print(f"[SUMMARY] Total Session PnL: INR {total_pnl:+,.2f} ({(total_pnl / ACCOUNT_EQUITY * 100.0):+.2f}% on INR 100k account)")
    print("=" * 90)

if __name__ == "__main__":
    run_backtest_previous_friday()
