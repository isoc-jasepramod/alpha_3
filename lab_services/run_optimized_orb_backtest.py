import sys
import os
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, time, timedelta
from typing import Dict, Any, List

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

def run_optimized_orb_backtest() -> Dict[str, Any]:
    print("\n" + "="*85)
    print("      PROJECT ALPHA 2.0 | OPTIMIZED ORB FOR OPTION BUYERS (PAST 1 MONTH)")
    print("="*85)

    df = yf.download('^NSEI', period='1mo', interval='5m', progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0].lower() for col in df.columns]
    else:
        df.columns = [col.lower() for col in df.columns]

    df = df.dropna()

    account_equity = 100000.0
    lot_size = 65
    max_risk_amount = 1000.0 # 1.0% equity

    trades_baseline = []
    trades_optimized = []

    daily_groups = df.groupby(df.index.date)

    for trade_date, day_df in daily_groups:
        day_df_ist = day_df.tz_convert('Asia/Kolkata') if day_df.index.tz is not None else day_df
        
        # 09:15 to 09:30 range (first 3 candles)
        orb_window = day_df_ist.between_time('09:15', '09:25')
        if len(orb_window) < 3:
            continue

        h_orb = float(orb_window['high'].max())
        l_orb = float(orb_window['low'].min())
        range_size = h_orb - l_orb

        # Skip dead flat days or gigantic gap anomalies
        if range_size < 15.0 or range_size > 250.0:
            continue

        buffer_delta = h_orb * 0.0003

        # Midpoint of ORB range
        mid_orb = (h_orb + l_orb) / 2.0

        scan_window = day_df_ist.between_time('09:30', '10:30')
        raw_trade = None

        for ts, bar in scan_window.iterrows():
            close_p = float(bar['close'])
            if close_p > (h_orb + buffer_delta):
                raw_trade = {
                    "direction": "CE_LONG",
                    "entry": close_p,
                    "h_orb": h_orb,
                    "l_orb": l_orb,
                    "range_size": range_size,
                    "mid_orb": mid_orb,
                    "time": ts.strftime('%H:%M'),
                    "date": trade_date.strftime('%Y-%m-%d'),
                    "entry_ts": ts
                }
                break
            elif close_p < (l_orb - buffer_delta):
                raw_trade = {
                    "direction": "PE_LONG",
                    "entry": close_p,
                    "h_orb": h_orb,
                    "l_orb": l_orb,
                    "range_size": range_size,
                    "mid_orb": mid_orb,
                    "time": ts.strftime('%H:%M'),
                    "date": trade_date.strftime('%Y-%m-%d'),
                    "entry_ts": ts
                }
                break

        if not raw_trade:
            continue

        entry_ts = raw_trade["entry_ts"]
        subsequent_bars = day_df_ist.loc[entry_ts:].iloc[1:]
        if len(subsequent_bars) == 0:
            continue

        # ==========================================
        # 1. BASELINE EXECUTION (Opposite SL, EOD 15:15)
        # ==========================================
        entry_b = raw_trade["entry"]
        if raw_trade["direction"] == "CE_LONG":
            sl_b = raw_trade["l_orb"]
            risk_b = entry_b - sl_b
            tgt_b = entry_b + (2.0 * risk_b)
        else:
            sl_b = raw_trade["h_orb"]
            risk_b = sl_b - entry_b
            tgt_b = entry_b - (2.0 * risk_b)

        outcome_b = "EOD_EXIT"
        exit_b = float(subsequent_bars.iloc[-1]['close'])

        for post_ts, pbar in subsequent_bars.iterrows():
            hp, lp = float(pbar['high']), float(pbar['low'])
            if raw_trade["direction"] == "CE_LONG":
                if hp >= tgt_b:
                    outcome_b = "TARGET_HIT"
                    exit_b = tgt_b
                    break
                elif lp <= sl_b:
                    outcome_b = "STOP_HIT"
                    exit_b = sl_b
                    break
            else:
                if lp <= tgt_b:
                    outcome_b = "TARGET_HIT"
                    exit_b = tgt_b
                    break
                elif hp >= sl_b:
                    outcome_b = "STOP_HIT"
                    exit_b = sl_b
                    break

        pts_b = (exit_b - entry_b) if raw_trade["direction"] == "CE_LONG" else (entry_b - exit_b)
        opt_pts_b = pts_b * 0.50
        lots_b = max(1, int(max_risk_amount / (risk_b * 0.50 * lot_size)))
        pnl_b = round(opt_pts_b * lots_b * lot_size, 2)

        trades_baseline.append({
            "date": raw_trade["date"],
            "type": raw_trade["direction"],
            "entry": round(entry_b, 1),
            "sl": round(sl_b, 1),
            "tgt": round(tgt_b, 1),
            "exit": round(exit_b, 1),
            "pts": round(pts_b, 1),
            "outcome": outcome_b,
            "pnl": pnl_b
        })

        # ==========================================
        # 2. OPTIMIZED EXECUTION:
        # - Half-Range (Midpoint) SL (cuts risk ~50%)
        # - 1:2 R:R Target is much closer (+50-60 pts)
        # - Move SL to Cost at 1:1 R:R
        # - 45-Minute Time Stop (exit after 9 candles if stagnant)
        # ==========================================
        entry_o = raw_trade["entry"]
        if raw_trade["direction"] == "CE_LONG":
            sl_o = raw_trade["mid_orb"]
            risk_o = entry_o - sl_o
            tgt_o = entry_o + (2.0 * risk_o)
            breakeven_trigger = entry_o + (1.0 * risk_o)
        else:
            sl_o = raw_trade["mid_orb"]
            risk_o = sl_o - entry_o
            tgt_o = entry_o - (2.0 * risk_o)
            breakeven_trigger = entry_o - (1.0 * risk_o)

        outcome_o = "TIME_STOP (45m)"
        exit_o = float(subsequent_bars.iloc[min(8, len(subsequent_bars)-1)]['close'])
        active_sl_o = sl_o
        breakeven_locked = False

        for i, (post_ts, pbar) in enumerate(subsequent_bars.iterrows()):
            hp, lp = float(pbar['high']), float(pbar['low'])
            cp = float(pbar['close'])

            # 45-min time stop (9 candles)
            if i >= 9:
                outcome_o = "TIME_STOP (45m)"
                exit_o = cp
                break

            if raw_trade["direction"] == "CE_LONG":
                # Check Breakeven lock at 1:1
                if not breakeven_locked and hp >= breakeven_trigger:
                    active_sl_o = entry_o
                    breakeven_locked = True

                # Check Target Hit
                if hp >= tgt_o:
                    outcome_o = "TARGET_HIT (1:2)"
                    exit_o = tgt_o
                    break
                # Check Stop Hit
                elif lp <= active_sl_o:
                    outcome_o = "BREAKEVEN_EXIT" if breakeven_locked else "STOP_HIT"
                    exit_o = active_sl_o
                    break
            else: # PE_LONG
                if not breakeven_locked and lp <= breakeven_trigger:
                    active_sl_o = entry_o
                    breakeven_locked = True

                if lp <= tgt_o:
                    outcome_o = "TARGET_HIT (1:2)"
                    exit_o = tgt_o
                    break
                elif hp >= active_sl_o:
                    outcome_o = "BREAKEVEN_EXIT" if breakeven_locked else "STOP_HIT"
                    exit_o = active_sl_o
                    break

        pts_o = (exit_o - entry_o) if raw_trade["direction"] == "CE_LONG" else (entry_o - exit_o)
        # Apply slight 15% theta penalty for trades held 45 mins on time stop
        opt_pts_o = pts_o * 0.50
        if "TIME_STOP" in outcome_o and opt_pts_o <= 0:
            opt_pts_o -= 3.0 # ~3 pts theta bleed

        lots_o = max(1, int(max_risk_amount / (risk_o * 0.50 * lot_size)))
        pnl_o = round(opt_pts_o * lots_o * lot_size, 2)

        trades_optimized.append({
            "date": raw_trade["date"],
            "type": raw_trade["direction"],
            "entry": round(entry_o, 1),
            "sl": round(sl_o, 1),
            "tgt": round(tgt_o, 1),
            "exit": round(exit_o, 1),
            "pts": round(pts_o, 1),
            "outcome": outcome_o,
            "pnl": pnl_o
        })

    df_base = pd.DataFrame(trades_baseline)
    df_opt = pd.DataFrame(trades_optimized)

    # Metrics Baseline
    wins_b = df_base[df_base["outcome"] == "TARGET_HIT"]
    losses_b = df_base[df_base["outcome"] == "STOP_HIT"]
    win_rate_b = (len(wins_b) / len(df_base)) * 100.0
    pnl_b_tot = df_base["pnl"].sum()
    pf_b = (df_base[df_base["pnl"] > 0]["pnl"].sum() / abs(df_base[df_base["pnl"] < 0]["pnl"].sum()))

    # Metrics Optimized
    wins_o = df_opt[df_opt["outcome"] == "TARGET_HIT (1:2)"]
    losses_o = df_opt[df_opt["outcome"] == "STOP_HIT"]
    be_o = df_opt[df_opt["outcome"] == "BREAKEVEN_EXIT"]
    time_o = df_opt[df_opt["outcome"].str.contains("TIME_STOP")]

    win_rate_o = (len(wins_o) / len(df_opt)) * 100.0
    pnl_o_tot = df_opt["pnl"].sum()
    gross_win_o = df_opt[df_opt["pnl"] > 0]["pnl"].sum()
    gross_loss_o = abs(df_opt[df_opt["pnl"] < 0]["pnl"].sum())
    pf_o = (gross_win_o / gross_loss_o) if gross_loss_o > 0 else 999.0

    print("\n" + "="*85)
    print(f"{'Performance Metric':<30} | {'Baseline ORB (Raw)':<24} | {'Optimized ORB (Upgraded)':<24}")
    print("="*85)
    print(f"{'Total Trades Triggered':<30} | {len(df_base):<24} | {len(df_opt):<24}")
    print(f"{'Target Hits (1:2 R:R)':<30} | {len(wins_b)} ({win_rate_b:.1f}%)" + " "*14 + f"| {len(wins_o)} ({win_rate_o:.1f}%)")
    print(f"{'Stop Loss Hits':<30} | {len(losses_b)} ({len(losses_b)/len(df_base)*100:.1f}%)" + " "*13 + f"| {len(losses_o)} ({len(losses_o)/len(df_opt)*100:.1f}%)")
    print(f"{'Breakeven Exits (1:1 lock)':<30} | {'0 (0.0%)':<24} | {len(be_o)} ({len(be_o)/len(df_opt)*100:.1f}%)")
    print(f"{'Time-Stop Exits (45 mins)':<30} | {'0 (Held to 15:15)':<24} | {len(time_o)} ({len(time_o)/len(df_opt)*100:.1f}%)")
    print(f"{'Win Rate (Target)':<30} | {win_rate_b:.2f}%" + " "*17 + f"| {win_rate_o:.2f}%")
    print(f"{'Profit Factor':<30} | {pf_b:.2f}" + " "*19 + f"| {pf_o:.2f}")
    print(f"{'Total Net Spot Points':<30} | {df_base['pts'].sum():+.1f} pts" + " "*15 + f"| {df_opt['pts'].sum():+.1f} pts")
    print(f"{'Net PnL (₹1,00,000 Equity)':<30} | INR {pnl_b_tot:+,.2f} ({pnl_b_tot/1000:+.2f}%)" + " "*4 + f"| INR {pnl_o_tot:+,.2f} ({pnl_o_tot/1000:+.2f}%)")
    print("="*85)

    print("\nUPGRADED TRADE LOG (HALF-RANGE SL + 45m TIME STOP + 1:1 BREAKEVEN):")
    print("-" * 100)
    print(f"{'Date':<11} {'Type':<8} {'Entry':<9} {'SL':<9} {'Target':<9} {'Exit':<9} {'Spot Pts':<10} {'Outcome':<20} {'PnL (INR)':<10}")
    print("-" * 100)
    for _, t in df_opt.iterrows():
        print(f"{t['date']:<11} {t['type']:<8} {t['entry']:<9} {t['sl']:<9} {t['tgt']:<9} {t['exit']:<9} {t['pts']:<10} {t['outcome']:<20} {t['pnl']:<10}")
    print("-" * 100)

if __name__ == "__main__":
    run_optimized_orb_backtest()
