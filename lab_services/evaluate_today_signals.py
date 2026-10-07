"""
PROJECT ALPHA 3.0 — DETAILED STRATEGY EXECUTION ON TODAY'S TICK DATA (2026-10-05)
Evaluates:
1. ORB Breakout (Did ATM Option RVOL filter save from the 09:30 trap?)
2. Momentum Impulse (13:41 Short-Covering Spike)
3. VWAP_EMA Alignment (Mid-day 11:00-12:30 Bear Trend & 13:40-14:30 Bull Reversal)
"""

import sys
import os
import json
from glob import glob
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
import pandas as pd
import numpy as np
from loguru import logger

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from backend.strategies.orb_breakout import VolumeBackedORB
from backend.strategies.vwap_ema import VWAPEMAAlignment
from backend.strategies.momentum_impulse import MomentumImpulseDetector
from backend.strategies.regime_filter import RegimeFilter
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
DELTA_ATM = 0.50

def run_detailed_evaluation():
    print("=" * 115)
    print("  PROJECT ALPHA 3.0 | TODAY (2026-10-05) IN-DEPTH STRATEGY EXECUTION AUDIT")
    print("=" * 115)

    # 1. Load Parquet Files
    partition_dir = os.path.join(project_root, "data", "lake", "date=2026-10-05")
    files = sorted(glob(os.path.join(partition_dir, "*.parquet")))
    print(f"Loading {len(files)} parquet files...")

    dfs = [pd.read_parquet(f) for f in files]
    df_all = pd.concat(dfs, ignore_index=True)

    first_ts = df_all["exchange_timestamp"].iloc[0]
    df_all["ts_sec"] = (df_all["exchange_timestamp"] / 1000.0) if first_ts > 1e11 else df_all["exchange_timestamp"].astype(float)
    IST = timezone(timedelta(hours=5, minutes=30))
    df_all["dt_ist"] = pd.to_datetime(df_all["ts_sec"], unit="s", utc=True).dt.tz_convert(IST)
    df_all["time_str"] = df_all["dt_ist"].dt.strftime("%H:%M:%S")

    mkt = df_all[(df_all["time_str"] >= "09:15:00") & (df_all["time_str"] <= "15:30:00")].copy()
    mkt.sort_values(by="ts_sec", inplace=True)
    print(f"Total Market Ticks: {len(mkt):,}")

    # Build 1m, 3m, 5m spot bars
    nifty_ticks = mkt[mkt["token"].isin(["99926000", 99926000, "26000", 26000])].set_index("dt_ist")
    sensex_ticks = mkt[mkt["token"].isin(["99919000", 99919000])].set_index("dt_ist")

    nifty_5m = nifty_ticks["ltp"].resample("5min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    sensex_5m = sensex_ticks["ltp"].resample("5min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()

    nifty_3m = nifty_ticks["ltp"].resample("3min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    sensex_3m = sensex_ticks["ltp"].resample("3min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()

    nifty_1m = nifty_ticks["ltp"].resample("1min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    sensex_1m = sensex_ticks["ltp"].resample("1min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()

    # Master contract lookup
    master_path = os.path.join(project_root, "data", "instrument_master.json")
    with open(master_path, "r", encoding="utf-8") as f:
        master_list = json.load(f)
    token_meta = {str(item["token"]): item for item in master_list}

    # =============================================================
    # AUDIT 1: VOLUME-BACKED ORB
    # =============================================================
    print("\n" + "=" * 115)
    print("  AUDIT 1: VOLUME-BACKED ORB BREAKOUT EVALUATION")
    print("=" * 115)

    for inst, df5, sym_tok in [("NIFTY", nifty_5m, "99926000"), ("SENSEX", sensex_5m, "99919000")]:
        orb_bars = df5.between_time("09:15", "09:29")
        h_orb = orb_bars["high"].max()
        l_orb = orb_bars["low"].min()
        orb_rng = h_orb - l_orb
        buffer = h_orb * 0.0003

        print(f"\n  [{inst}] 09:15 - 09:30 Opening Range:")
        print(f"    High: {h_orb:.2f} | Low: {l_orb:.2f} | Range: {orb_rng:.2f} pts")

        # Scan 09:30 - 10:30
        scan = df5.between_time("09:30", "10:30")
        naive_breakout = None
        for ts, r in scan.iterrows():
            c = r["close"]
            if c > (h_orb + buffer):
                naive_breakout = ("CE", ts.strftime("%H:%M"), c)
                break
            elif c < (l_orb - buffer):
                naive_breakout = ("PE", ts.strftime("%H:%M"), c)
                break

        if naive_breakout:
            b_dir, b_time, b_price = naive_breakout
            print(f"    🚨 Naive Spot Breakout: {b_dir} at {b_time} (Spot: {b_price:.2f})")
            
            # Check ATM Option Volume in Lake around breakout
            atm_strike = round(b_price / (50.0 if inst == "NIFTY" else 100.0)) * (50.0 if inst == "NIFTY" else 100.0)
            print(f"    Checking Real ATM Option ({inst} {atm_strike:.0f} {b_dir}) traded volume...")
            
            # Find active weekly option token (06OCT26 for NIFTY, 08OCT26 for SENSEX)
            match_tok = None
            expiry_tag = "06OCT26" if inst == "NIFTY" else "26O08"
            for tok, item in token_meta.items():
                if item.get("name") == inst and item.get("instrumenttype") == "OPTIDX":
                    sym = item.get("symbol", "")
                    raw_strk = float(item.get("strike", 0.0))
                    strk = raw_strk / 100.0 if raw_strk > 100000 else raw_strk
                    if abs(strk - atm_strike) < 1.0 and sym.endswith(b_dir) and expiry_tag in sym:
                        match_tok = tok
                        break

            if match_tok:
                opt_sub = mkt[mkt["token"].astype(str) == match_tok].copy()
                opt_sub.set_index("dt_ist", inplace=True)
                opt_5m = opt_sub["volume"].resample("5min").last().diff().dropna()
                sym_name = token_meta[match_tok]["symbol"]
                print(f"    Matched Active Weekly Token: {match_tok} ({sym_name})")
                
                # Check option volume surge
                try:
                    vol_sub = opt_5m[opt_5m.index.strftime("%H:%M") == b_time]
                    vol_at_breakout = vol_sub.iloc[0] if not vol_sub.empty else 0.0
                    prior_vols = opt_5m[opt_5m.index.strftime("%H:%M") < b_time]
                    baseline = prior_vols.mean() if len(prior_vols) > 0 else 0
                    rvol = (vol_at_breakout / baseline) if baseline > 0 else 0.0
                    print(f"    Option 5-min Delta Volume: {vol_at_breakout:,.0f} vs Baseline: {baseline:,.0f} -> RVOL = {rvol:.2f}x")
                    if rvol >= 1.80:
                        print(f"    STATUS: CONFIRMED by Option RVOL ({rvol:.2f}x >= 1.80x)")
                    else:
                        print(f"    STATUS: 🛡️ BLOCKED by Option RVOL ({rvol:.2f}x < 1.80x) — FAKEOUT TRAP AVOIDED!")
                except Exception as e:
                    print(f"    Option Volume check: {e}")

            # Trace what happened to spot after naive breakout
            sub_after = df5[df5.index.strftime("%H:%M") >= b_time]
            min_after = sub_after["low"].min()
            max_after = sub_after["high"].max()
            sl = b_price - (0.5 * orb_rng) if b_dir == "CE" else b_price + (0.5 * orb_rng)
            stopped_out = (min_after <= sl) if b_dir == "CE" else (max_after >= sl)
            print(f"    Post-Breakout Spot Move: Min {min_after:.2f}, Max {max_after:.2f}")
            print(f"    Stop Loss level was {sl:.2f}. Would Naive Spot get stopped out? {'YES (Trap confirmed!)' if stopped_out else 'NO'}")
        else:
            print("    No Breakout between 09:30 and 10:30.")

    # =============================================================
    # AUDIT 2: MOMENTUM IMPULSE DETECTOR
    # =============================================================
    print("\n" + "=" * 115)
    print("  AUDIT 2: MOMENTUM IMPULSE DETECTOR (FAST VELOCITY SPIKES)")
    print("=" * 115)

    for inst, df1 in [("NIFTY", nifty_1m), ("SENSEX", sensex_1m)]:
        thresh = 25.0 if inst == "NIFTY" else 75.0
        impulses = []
        for i in range(3, len(df1)):
            delta = df1["close"].iloc[i] - df1["close"].iloc[i-3]
            t_str = df1.index[i].strftime("%H:%M")
            if t_str < "09:30" or t_str > "15:15":
                continue
            if abs(delta) >= thresh:
                direction = "CE" if delta > 0 else "PE"
                impulses.append((t_str, direction, delta, df1["close"].iloc[i]))

        print(f"\n  [{inst}] Momentum Impulses Detected (Threshold >= ±{thresh} pts in 3 min): {len(impulses)}")
        for t_str, direction, delta, price in impulses:
            print(f"    ⚡ {t_str} | {direction} Impulse: {delta:+6.2f} pts | Spot: {price:.2f}")

    # =============================================================
    # AUDIT 3: VWAP_EMA ALIGNMENT
    # =============================================================
    print("\n" + "=" * 115)
    print("  AUDIT 3: VWAP_EMA PULLBACK & TREND CONTINUATION SETUPS")
    print("=" * 115)

    for inst, df3 in [("NIFTY", nifty_3m), ("SENSEX", sensex_3m)]:
        print(f"\n  [{inst}] VWAP_EMA Setups:")
        ema9 = IncrementalEMA(period=9)
        ema21 = IncrementalEMA(period=21)
        vwap = IncrementalVWAP()
        adx = IncrementalADX(period=14)
        rsi = IncrementalRSI(period=14)
        rf = RegimeFilter()

        vwap_trades = []
        awaiting = None

        for ts, r in df3.iterrows():
            t_str = ts.strftime("%H:%M")
            o, h, l, c = r["open"], r["high"], r["low"], r["close"]
            e9_val = ema9.update(c)
            e21_val = ema21.update(c)
            v_val = vwap.update((h + l + c) / 3.0, 1000.0)
            adx_val = adx.update(h, l, c)
            rsi_val = rsi.update(c)

            st = rf.update_candle(inst, {"open": o, "high": h, "low": l, "close": c}, v_val, adx_val)

            # Check awaiting confirmation
            if awaiting:
                aw_dir = awaiting["direction"]
                confirmed = False
                if aw_dir == "CE" and c > e9_val and c >= o:
                    confirmed = True
                    sl = min(awaiting["low"], l, e9_val) - 5.0
                    risk = c - sl
                    vwap_trades.append({"time": t_str, "dir": "CE", "entry": c, "sl": sl, "risk": risk, "t1": c + risk, "t2": c + 2.0*risk, "post": df3.loc[ts:]})
                elif aw_dir == "PE" and c < e9_val and c <= o:
                    confirmed = True
                    sl = max(awaiting["high"], h, e9_val) + 5.0
                    risk = sl - c
                    vwap_trades.append({"time": t_str, "dir": "PE", "entry": c, "sl": sl, "risk": risk, "t1": c - risk, "t2": c - 2.0*risk, "post": df3.loc[ts:]})
                awaiting = None

            if t_str < "09:30" or t_str > "15:00":
                continue

            bot_wick = calculate_bottom_wick_ratio(o, h, l, c)
            top_wick = calculate_top_wick_ratio(o, h, l, c)

            if adx_val >= 22.0:
                if e9_val > e21_val and c > v_val and bot_wick >= 0.35 and 48.0 <= rsi_val <= 72.0:
                    awaiting = {"direction": "CE", "low": l, "high": h, "time": t_str}
                elif e9_val < e21_val and c < v_val and top_wick >= 0.15 and 18.0 <= rsi_val <= 52.0:
                    awaiting = {"direction": "PE", "low": l, "high": h, "time": t_str}

        print(f"    Confirmed VWAP_EMA Trades: {len(vwap_trades)}")
        for t in vwap_trades:
            # Resolve against post data
            post = t["post"].iloc[1:]
            entry = t["entry"]
            sl = t["sl"]
            t1 = t["t1"]
            t2 = t["t2"]
            direction = t["dir"]

            outcome = "OPEN/EOD"
            for _, pr in post.iterrows():
                ph, pl = pr["high"], pr["low"]
                if direction == "CE":
                    if pl <= sl:
                        outcome = "STOP_HIT"
                        break
                    elif ph >= t2:
                        outcome = "T2_HIT (+2R)"
                        break
                    elif ph >= t1:
                        outcome = "T1_HIT (+1R)"
                elif direction == "PE":
                    if ph >= sl:
                        outcome = "STOP_HIT"
                        break
                    elif pl <= t2:
                        outcome = "T2_HIT (+2R)"
                        break
                    elif pl <= t1:
                        outcome = "T1_HIT (+1R)"

            print(f"    🎯 {t['time']} | {direction} Entry: {entry:.2f} | SL: {sl:.2f} | Risk: {t['risk']:.1f} pts | Outcome: {outcome}")

    print("\n" + "=" * 115)

if __name__ == "__main__":
    run_detailed_evaluation()
