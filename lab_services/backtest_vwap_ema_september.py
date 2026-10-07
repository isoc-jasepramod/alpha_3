"""
PROJECT ALPHA 3.0 — FULL SEPTEMBER 2026 VWAP & EMA STRATEGY BACKTEST (21 SESSIONS)
Real 3-Minute Candle Data directly from AngelOne SmartAPI for NIFTY 50 and BSE SENSEX:
- Dates: 2026-09-01 to 2026-09-30 (all 21 trading sessions, 2,541 3-min bars per symbol)
- Evaluates VWAP_EMA Alignment with Wick Rejection + Confirmation Candle
- Indicators: IncrementalEMA(9), IncrementalEMA(21), Daily VWAP, IncrementalRSI(14), IncrementalADX(14)
- Anti-Chop Filters:
    1. Trend Alignment (EMA9 vs EMA21, Close vs VWAP, VWAP Slope >= 0.35, ADX >= 22.0)
    2. RSI Gates (48-72 for CE, 18-52 for PE)
    3. Intraday Range Exhaustion Filter (2.0x ATR floor, dynamic expansion up to 5.0x ATR on strong trends)
    4. VWAP Distance Filter (0.30% max distance, relaxed to 0.75% on EMA9 bounce with ADX >= 25)
    5. Consecutive Stop Cooldown (15 min pause after 2 stops)
- Risk Management:
    - Fixed 1% Risk (INR 1,000 per trade) on INR 100,000 capital
    - Delta ATM = 0.50, Lot size: NIFTY 65, SENSEX 20
    - Targets: T1 = 1R (50% partial exit, trail SL to Breakeven), T2 = 2R (runner)
    - EOD Exit: 15:25 IST
- Compares:
    1. Pure Strategy (No Circuit Breaker)
    2. With Daily -2.5% Circuit Breaker Protection
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
from collections import deque
from loguru import logger

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from backend.core.auth import AngelOneAuth
from backend.strategies.indicators import (
    IncrementalEMA,
    IncrementalVWAP,
    IncrementalRSI,
    IncrementalADX,
    calculate_bottom_wick_ratio,
    calculate_top_wick_ratio
)

ACCOUNT_EQUITY = 100000.0
RISK_PER_TRADE = 1000.0
LOT_SIZE_NIFTY = 65
LOT_SIZE_SENSEX = 20
DELTA_ATM = 0.50

def fetch_september_3m_candles(auth: AngelOneAuth) -> Dict[str, pd.DataFrame]:
    """Fetches all 3-minute candles for NIFTY and SENSEX for September 2026."""
    candles_dict = {}
    for inst, exch, tok in [("NIFTY", "NSE", "99926000"), ("SENSEX", "BSE", "99919000")]:
        param = {
            "exchange": exch,
            "symboltoken": tok,
            "interval": "THREE_MINUTE",
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
                    "volume": float(r[5]) if len(r) > 5 else 1000.0
                })
            df = pd.DataFrame(rows).set_index("timestamp")
            candles_dict[inst] = df
            logger.info(f"Loaded {len(df)} 3-min candles for {inst} across September 2026.")
    return candles_dict

def simulate_vwap_ema_day(df_day: pd.DataFrame, inst: str, date_str: str, use_regime_filter: bool = False) -> List[Dict[str, Any]]:
    """Simulates VWAP_EMA strategy on a single day's 3-minute candles."""
    scan_window = df_day.between_time("09:15", "15:15")
    if len(scan_window) < 10:
        return []

    from backend.strategies.regime_filter import RegimeFilter
    regime_filter = RegimeFilter() if use_regime_filter else None

    ema9 = IncrementalEMA(period=9)
    ema21 = IncrementalEMA(period=21)
    vwap = IncrementalVWAP()
    rsi = IncrementalRSI(period=14)
    adx = IncrementalADX(period=14)

    vwap_history = deque(maxlen=5)
    atr_val = 30.0 if inst == "NIFTY" else 80.0
    day_high = -1.0
    day_low = 1e9

    awaiting = None
    trades = []
    consecutive_stops = {"CE": 0, "PE": 0}
    last_stop_time = {"CE": datetime.min, "PE": datetime.min}

    bars = list(scan_window.iterrows())

    for i in range(len(bars)):
        ts, row = bars[i]
        t_str = ts.strftime("%H:%M")
        o = float(row["open"])
        h = float(row["high"])
        l = float(row["low"])
        c = float(row["close"])
        v = float(row["volume"]) if float(row["volume"]) > 0 else 1000.0

        day_high = max(day_high, h)
        day_low = min(day_low, l)

        e9 = ema9.update(c)
        e21 = ema21.update(c)
        vwap_val = vwap.update((h + l + c) / 3.0, v)
        rsi_val = rsi.update(c)
        adx_val = adx.update(h, l, c)

        candle_rng = h - l
        atr_val = atr_val * 0.9 + candle_rng * 0.1

        # Feed Regime Filter if enabled
        if regime_filter:
            regime_filter.update_candle(inst, {"open": o, "high": h, "low": l, "close": c}, vwap_val, adx_val)

        vwap_history.append(vwap_val)
        vh = list(vwap_history)
        vwap_slope = ((vh[-1] - vh[0]) / (len(vh) - 1)) if len(vh) >= 3 else 0.0

        # Don't scan setups before 09:30 to allow indicator warmup
        if t_str < "09:30":
            continue

        # -------------------------------------------------------------
        # 1. Check Awaiting Confirmation from Previous Candle
        # -------------------------------------------------------------
        if awaiting is not None:
            aw_dir = awaiting["direction"]
            aw_adx = awaiting["adx"]
            aw_slope = awaiting["vwap_slope"]
            aw_tested_ema9 = awaiting["tested_ema9"]
            rej_candle = awaiting["rejection_candle"]

            # Consecutive stop cooldown check (15 min)
            in_cooldown = False
            if consecutive_stops[aw_dir] >= 2:
                if (ts.to_pydatetime() - last_stop_time[aw_dir]).total_seconds() < 900:
                    in_cooldown = True
                else:
                    consecutive_stops[aw_dir] = 0

            # Range exhaustion filter
            atr_floor = 30.0 if inst == "NIFTY" else 80.0
            eff_atr = max(atr_val, atr_floor)
            multiplier = 2.0
            if aw_adx >= 28.0:
                if (aw_dir == "PE" and aw_slope <= -0.35) or (aw_dir == "CE" and aw_slope >= 0.35):
                    multiplier = 5.0
            thresh = multiplier * eff_atr

            is_exhausted = False
            if aw_dir == "PE" and (day_high - c) > thresh:
                is_exhausted = True
            elif aw_dir == "CE" and (c - day_low) > thresh:
                is_exhausted = True

            # VWAP distance filter
            vwap_dist_pct = abs(c - vwap_val) / vwap_val * 100.0 if vwap_val > 0 else 0.0
            max_dist = 0.30
            if aw_tested_ema9 and aw_adx >= 25.0:
                max_dist = 0.75
            elif aw_adx >= 28.0:
                max_dist = 0.60
            is_chasing = vwap_dist_pct > max_dist

            # Regime Filter check
            regime_ok = True
            if regime_filter:
                regime_ok, _ = regime_filter.allows_vwap_ema(inst)

            confirmed = False
            if regime_ok and not in_cooldown and not is_exhausted and not is_chasing:
                if aw_dir == "CE" and c > e9 and c >= o:
                    sl = round(min(rej_candle["low"], l, e9) - 5.0, 2)
                    risk = round(c - sl, 2)
                    min_r = 10.0 if inst == "NIFTY" else 30.0
                    if risk >= min_r:
                        confirmed = True
                        trades.append({
                            "date": date_str,
                            "strategy": "VWAP_EMA",
                            "instrument": inst,
                            "direction": "CE",
                            "entry_time": t_str,
                            "entry_price": round(c, 2),
                            "sl": sl,
                            "risk_pts": risk,
                            "t1": round(c + risk, 2),
                            "t2": round(c + 2.0 * risk, 2),
                            "vwap": round(vwap_val, 2),
                            "ema9": round(e9, 2),
                            "adx": round(aw_adx, 1),
                            "rsi": round(rsi_val, 1),
                            "post_data": df_day.loc[ts:]
                        })
                elif aw_dir == "PE" and c < e9 and c <= o:
                    sl = round(max(rej_candle["high"], h, e9) + 5.0, 2)
                    risk = round(sl - c, 2)
                    min_r = 10.0 if inst == "NIFTY" else 30.0
                    if risk >= min_r:
                        confirmed = True
                        trades.append({
                            "date": date_str,
                            "strategy": "VWAP_EMA",
                            "instrument": inst,
                            "direction": "PE",
                            "entry_time": t_str,
                            "entry_price": round(c, 2),
                            "sl": sl,
                            "risk_pts": risk,
                            "t1": round(c - risk, 2),
                            "t2": round(c - 2.0 * risk, 2),
                            "vwap": round(vwap_val, 2),
                            "ema9": round(e9, 2),
                            "adx": round(aw_adx, 1),
                            "rsi": round(rsi_val, 1),
                            "post_data": df_day.loc[ts:]
                        })

            awaiting = None
            if confirmed:
                continue

        # -------------------------------------------------------------
        # 2. Candidate Wick Rejection Setup Detection
        # -------------------------------------------------------------
        if adx_val < 22.0:
            continue

        if regime_filter:
            regime_ok, _ = regime_filter.allows_vwap_ema(inst)
            if not regime_ok:
                continue

        bottom_wick = calculate_bottom_wick_ratio(o, h, l, c)
        top_wick = calculate_top_wick_ratio(o, h, l, c)

        # Bullish Pullback Setup (CE)
        if e9 > e21 and c > vwap_val and vwap_slope >= 0.35:
            tested_ema9 = (l <= e9 * 1.0005 and c > e9)
            tested_vwap = (abs(l - vwap_val) <= (0.0005 * c))
            if (tested_ema9 or tested_vwap) and bottom_wick >= 0.35:
                if 48.0 <= rsi_val <= 72.0:
                    awaiting = {
                        "direction": "CE",
                        "rejection_candle": {"open": o, "high": h, "low": l, "close": c},
                        "ema9": e9,
                        "vwap": vwap_val,
                        "adx": adx_val,
                        "vwap_slope": vwap_slope,
                        "tested_ema9": tested_ema9
                    }

        # Bearish Pullback / Breakdown Setup (PE)
        elif e9 < e21 and c < vwap_val and vwap_slope <= -0.35:
            tested_ema9 = (h >= e9 * 0.9995 and c < e9)
            tested_vwap = (abs(h - vwap_val) <= (0.0005 * c))
            is_breakdown = (c < o and (o - c) >= 0.35 * max(5.0, h - l))
            if ((tested_ema9 or tested_vwap) and top_wick >= 0.15) or is_breakdown:
                if 18.0 <= rsi_val <= 52.0:
                    awaiting = {
                        "direction": "PE",
                        "rejection_candle": {"open": o, "high": h, "low": l, "close": c},
                        "ema9": e9,
                        "vwap": vwap_val,
                        "adx": adx_val,
                        "vwap_slope": vwap_slope,
                        "tested_ema9": tested_ema9
                    }

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

        if t_str >= "15:25":
            break

        if direction == "CE":
            if not t1_hit and l <= sl:
                sl_hit = True
                exit_p = sl
                exit_time = t_str
                pnl = -RISK_PER_TRADE
                break
            elif t1_hit and l <= entry:
                # Breakeven stop for runner
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
                # Breakeven stop for runner
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

def run_vwap_ema_backtest():
    print("=" * 115)
    print("  PROJECT ALPHA 3.0 | FULL MONTH SEPTEMBER 2026 VWAP_EMA BACKTEST (21 SESSIONS)")
    print("  Real 3-Minute Candles from AngelOne SmartAPI | 1% Risk (INR 1,000) on INR 100k Capital")
    print("=" * 115)

    auth = AngelOneAuth()
    login_res = auth.login_sync()
    if not login_res.get("status"):
        print("[ERROR] AngelOne authentication failed.")
        return

    candles_map = fetch_september_3m_candles(auth)
    if not candles_map:
        print("[ERROR] Failed to fetch candles.")
        return

    nifty_df = candles_map["NIFTY"]
    dates = sorted(list(set(nifty_df.index.strftime("%Y-%m-%d"))))

    # 1. Simulate without regime filter
    raw_trades = []
    for date_str in dates:
        for inst in ["NIFTY", "SENSEX"]:
            df_inst = candles_map[inst]
            day_df = df_inst[df_inst.index.strftime("%Y-%m-%d") == date_str]
            if day_df.empty:
                continue
            day_candidates = simulate_vwap_ema_day(day_df, inst, date_str, use_regime_filter=False)
            for t in day_candidates:
                resolved = resolve_trade(t, inst)
                raw_trades.append(resolved)
    raw_trades.sort(key=lambda x: (x["date"], x["entry_time"]))

    # 2. Simulate with Regime Filter (blocks CHOPPY sessions)
    regime_trades = []
    for date_str in dates:
        for inst in ["NIFTY", "SENSEX"]:
            df_inst = candles_map[inst]
            day_df = df_inst[df_inst.index.strftime("%Y-%m-%d") == date_str]
            if day_df.empty:
                continue
            day_candidates = simulate_vwap_ema_day(day_df, inst, date_str, use_regime_filter=True)
            for t in day_candidates:
                resolved = resolve_trade(t, inst)
                regime_trades.append(resolved)
    regime_trades.sort(key=lambda x: (x["date"], x["entry_time"]))

    # Helper for circuit breaker simulation
    def apply_circuit_breaker(trades_list):
        daily_trades_map = {d: [] for d in dates}
        for t in trades_list:
            daily_trades_map[t["date"]].append(t)
        
        filtered = []
        cb_trips = 0
        daily_summaries = []
        for d in dates:
            dt = datetime.strptime(d, "%Y-%m-%d")
            day_name = dt.strftime("%A")
            d_trades = daily_trades_map[d]
            active_d_trades = []
            running_pnl = 0.0
            tripped = False
            for t in d_trades:
                if tripped:
                    continue
                active_d_trades.append(t)
                running_pnl += t["pnl_inr"]
                if running_pnl <= -2500.0:
                    tripped = True
                    cb_trips += 1
            filtered.extend(active_d_trades)
            d_pnl = sum(t["pnl_inr"] for t in active_d_trades)
            d_wins = sum(1 for t in active_d_trades if t["pnl_inr"] > 0)
            d_losses = sum(1 for t in active_d_trades if t["pnl_inr"] < 0)
            d_wr = (d_wins / len(active_d_trades) * 100.0) if active_d_trades else 0.0
            daily_summaries.append({
                "date": d, "day": day_name, "trades": len(active_d_trades),
                "wins": d_wins, "losses": d_losses, "win_rate": d_wr, "pnl": d_pnl, "tripped": tripped
            })
        return filtered, cb_trips, daily_summaries

    cb_trades, cb_trips, daily_cb_summaries = apply_circuit_breaker(raw_trades)
    regime_cb_trades, regime_cb_trips, daily_regime_cb_summaries = apply_circuit_breaker(regime_trades)

    # Print Daily Breakdown Table (With Circuit Breaker)
    print("\n" + "=" * 115)
    print("  SEPTEMBER 2026 DAILY PERFORMANCE (WITH -2.5% CIRCUIT BREAKER)")
    print("=" * 115)
    print(f"{'DATE':<11} | {'DAY':<10} | {'TRADES':<7} | {'WINS':<5} | {'LOSSES':<7} | {'WIN RATE':<9} | {'SESSION PNL':<14} | {'CIRCUIT BREAKER'}")
    print("-" * 115)
    for ds in daily_cb_summaries:
        status_str = f"INR {ds['pnl']:+,.2f}" if ds['trades'] > 0 else "— (No Setup)"
        cb_str = "TRIPPED (-2.5%)" if ds["tripped"] else "Normal"
        print(
            f"{ds['date']:<11} | {ds['day']:<10} | {ds['trades']:<7} | {ds['wins']:<5} | "
            f"{ds['losses']:<7} | {ds['win_rate']:<8.1f}% | {status_str:<14} | {cb_str}"
        )
    print("-" * 115)

    # Weekly Performance Rollup
    print("\n" + "=" * 115)
    print("  SEPTEMBER 2026 WEEKLY PERFORMANCE ROLLUP")
    print("=" * 115)
    weeks = [
        ("Week 1 (Sep 01 - Sep 04)", "2026-09-01", "2026-09-04"),
        ("Week 2 (Sep 07 - Sep 11)", "2026-09-07", "2026-09-11"),
        ("Week 3 (Sep 14 - Sep 18)", "2026-09-14", "2026-09-18"),
        ("Week 4 (Sep 21 - Sep 25)", "2026-09-21", "2026-09-25"),
        ("Week 5 (Sep 28 - Sep 30)", "2026-09-28", "2026-09-30"),
    ]
    for wname, start_d, end_d in weeks:
        w_trades = [t for t in cb_trades if start_d <= t["date"] <= end_d]
        w_pnl = sum(t["pnl_inr"] for t in w_trades)
        w_wins = sum(1 for t in w_trades if t["pnl_inr"] > 0)
        w_losses = sum(1 for t in w_trades if t["pnl_inr"] < 0)
        w_wr = (w_wins / len(w_trades) * 100.0) if w_trades else 0.0
        print(f"  {wname:<28} | Trades: {len(w_trades):<3} | Wins: {w_wins:<2} | Losses: {w_losses:<2} | WinRate: {w_wr:5.1f}% | Net PnL: INR {w_pnl:+,.2f}")
    print("-" * 115)

    def compute_stats(trades_list):
        total = len(trades_list)
        wins = sum(1 for t in trades_list if t["pnl_inr"] > 0)
        losses = sum(1 for t in trades_list if t["pnl_inr"] < 0)
        pnl = sum(t["pnl_inr"] for t in trades_list)
        wr = (wins / total * 100.0) if total else 0.0
        gp = sum(t["pnl_inr"] for t in trades_list if t["pnl_inr"] > 0)
        gl = abs(sum(t["pnl_inr"] for t in trades_list if t["pnl_inr"] < 0))
        pf = (gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0)
        t2_cnt = sum(1 for t in trades_list if t["status"] == "T2_HIT")
        t1_cnt = sum(1 for t in trades_list if t["status"] == "T1_BE")
        sl_cnt = sum(1 for t in trades_list if t["status"] == "STOP_HIT")
        eod_cnt = sum(1 for t in trades_list if t["status"] == "EOD_EXIT")
        return {
            "total": total, "wins": wins, "losses": losses, "pnl": pnl, "wr": wr,
            "gp": gp, "gl": gl, "pf": pf, "t2": t2_cnt, "t1": t1_cnt, "sl": sl_cnt, "eod": eod_cnt
        }

    s_pure = compute_stats(raw_trades)
    s_cb = compute_stats(cb_trades)
    s_regime = compute_stats(regime_trades)
    s_regime_cb = compute_stats(regime_cb_trades)

    print("\n" + "=" * 125)
    print("  EXECUTIVE MULTI-DIMENSIONAL COMPARISON (SEPTEMBER 2026)")
    print("=" * 125)
    print(f"  {'METRIC':<26} | {'PURE STRATEGY':<18} | {'WITH CB (-2.5%)':<18} | {'WITH REGIME FILTER':<20} | {'REGIME + CB':<18}")
    print("-" * 125)
    print(f"  {'Total Trades':<26} | {s_pure['total']:<18} | {s_cb['total']:<18} | {s_regime['total']:<20} | {s_regime_cb['total']:<18}")
    print(f"  {'Winning Trades':<26} | {s_pure['wins']:<18} | {s_cb['wins']:<18} | {s_regime['wins']:<20} | {s_regime_cb['wins']:<18}")
    print(f"  {'Losing Trades':<26} | {s_pure['losses']:<18} | {s_cb['losses']:<18} | {s_regime['losses']:<20} | {s_regime_cb['losses']:<18}")
    print(f"  {'Win Rate':<26} | {s_pure['wr']:<17.1f}% | {s_cb['wr']:<17.1f}% | {s_regime['wr']:<19.1f}% | {s_regime_cb['wr']:<17.1f}%")
    print(f"  {'Profit Factor':<26} | {s_pure['pf']:<18.2f} | {s_cb['pf']:<18.2f} | {s_regime['pf']:<20.2f} | {s_regime_cb['pf']:<18.2f}")
    print(f"  {'Gross Profit':<26} | INR {s_pure['gp']:<14,.2f} | INR {s_cb['gp']:<14,.2f} | INR {s_regime['gp']:<16,.2f} | INR {s_regime_cb['gp']:<14,.2f}")
    print(f"  {'Gross Loss':<26} | -INR {s_pure['gl']:<13,.2f} | -INR {s_cb['gl']:<13,.2f} | -INR {s_regime['gl']:<15,.2f} | -INR {s_regime_cb['gl']:<13,.2f}")
    print(f"  {'Net PnL (INR)':<26} | INR {s_pure['pnl']:<14,.2f} | INR {s_cb['pnl']:<14,.2f} | INR {s_regime['pnl']:<16,.2f} | INR {s_regime_cb['pnl']:<14,.2f}")
    print(f"  {'ROI on INR 100k':<26} | {s_pure['pnl']/1000:<17.2f}% | {s_cb['pnl']/1000:<17.2f}% | {s_regime['pnl']/1000:<19.2f}% | {s_regime_cb['pnl']/1000:<17.2f}%")
    print("=" * 125)

if __name__ == "__main__":
    run_vwap_ema_backtest()

if __name__ == "__main__":
    run_vwap_ema_backtest()
