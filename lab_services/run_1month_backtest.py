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

def run_orb_backtest_1month() -> Dict[str, Any]:
    print("\n" + "="*80)
    print("  PROJECT ALPHA 2.0 | 1-MONTH HISTORICAL SPOT BACKTEST (REAL NIFTY 50 DATA)")
    print("="*80)
    print("Fetching past 1 month of 5-minute intraday data for NIFTY 50 (^NSEI)...")

    df = yf.download('^NSEI', period='1mo', interval='5m', progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0].lower() for col in df.columns]
    else:
        df.columns = [col.lower() for col in df.columns]

    df = df.dropna()
    print(f"Loaded {len(df)} candles across dates: {df.index[0].strftime('%Y-%m-%d')} to {df.index[-1].strftime('%Y-%m-%d')}")

    trades = []
    daily_groups = df.groupby(df.index.date)

    account_equity = 100000.0
    risk_per_trade_pct = 0.01 # 1.0% risk = ₹1,000
    lot_size = 65

    for trade_date, day_df in daily_groups:
        # Timezone conversion if needed
        # Intraday slice 09:15 to 09:30 (first 3 candles: 09:15, 09:20, 09:25)
        day_df_ist = day_df.tz_convert('Asia/Kolkata') if day_df.index.tz is not None else day_df
        
        orb_window = day_df_ist.between_time('09:15', '09:25')
        if len(orb_window) < 3:
            continue

        h_orb = float(orb_window['high'].max())
        l_orb = float(orb_window['low'].min())
        range_size = h_orb - l_orb

        # If opening range is too tiny or massive, skip
        if range_size < 15.0 or range_size > 250.0:
            continue

        buffer_delta = h_orb * 0.0003 # delta = Spot * 0.0003 (~7-8 pts)

        # Breakout scan window: 09:30 to 10:30
        scan_window = day_df_ist.between_time('09:30', '10:30')
        active_trade = None

        for ts, bar in scan_window.iterrows():
            close_p = float(bar['close'])
            
            # CE Long Breakout
            if close_p > (h_orb + buffer_delta):
                entry_p = close_p
                # Stop loss at ORB Low or 1.5x range
                sl_p = l_orb
                spot_risk = entry_p - sl_p
                if spot_risk <= 0:
                    continue
                # Option equivalent math: assume ~0.5 delta
                # Fixed 1:2 Risk to Reward
                tgt_p = entry_p + (2.0 * spot_risk)

                active_trade = {
                    "date": trade_date.strftime('%Y-%m-%d'),
                    "time": ts.strftime('%H:%M'),
                    "direction": "CE_LONG",
                    "entry": round(entry_p, 1),
                    "sl": round(sl_p, 1),
                    "tgt": round(tgt_p, 1),
                    "risk_pts": round(spot_risk, 1),
                    "entry_ts": ts
                }
                break

            # PE Short Breakdown
            elif close_p < (l_orb - buffer_delta):
                entry_p = close_p
                sl_p = h_orb
                spot_risk = sl_p - entry_p
                if spot_risk <= 0:
                    continue
                tgt_p = entry_p - (2.0 * spot_risk)

                active_trade = {
                    "date": trade_date.strftime('%Y-%m-%d'),
                    "time": ts.strftime('%H:%M'),
                    "direction": "PE_LONG",
                    "entry": round(entry_p, 1),
                    "sl": round(sl_p, 1),
                    "tgt": round(tgt_p, 1),
                    "risk_pts": round(spot_risk, 1),
                    "entry_ts": ts
                }
                break

        if active_trade:
            # Track subsequent bars until 15:15 for outcome
            entry_ts = active_trade["entry_ts"]
            subsequent_bars = day_df_ist.loc[entry_ts:].iloc[1:]
            
            outcome = "EOD_EXIT"
            exit_p = float(subsequent_bars.iloc[-1]['close']) if len(subsequent_bars) > 0 else active_trade["entry"]
            exit_time = "15:15"

            for post_ts, pbar in subsequent_bars.iterrows():
                high_p = float(pbar['high'])
                low_p = float(pbar['low'])

                if active_trade["direction"] == "CE_LONG":
                    if high_p >= active_trade["tgt"]:
                        outcome = "TARGET_HIT (1:2)"
                        exit_p = active_trade["tgt"]
                        exit_time = post_ts.strftime('%H:%M')
                        break
                    elif low_p <= active_trade["sl"]:
                        outcome = "STOP_HIT"
                        exit_p = active_trade["sl"]
                        exit_time = post_ts.strftime('%H:%M')
                        break
                else: # PE_LONG
                    if low_p <= active_trade["tgt"]:
                        outcome = "TARGET_HIT (1:2)"
                        exit_p = active_trade["tgt"]
                        exit_time = post_ts.strftime('%H:%M')
                        break
                    elif high_p >= active_trade["sl"]:
                        outcome = "STOP_HIT"
                        exit_p = active_trade["sl"]
                        exit_time = post_ts.strftime('%H:%M')
                        break

            # Calculate points and Option PnL
            # Assuming ATM delta ~0.50 for option equivalent return
            if active_trade["direction"] == "CE_LONG":
                pts = exit_p - active_trade["entry"]
            else:
                pts = active_trade["entry"] - exit_p

            opt_pts = pts * 0.50 # 50 Delta option equivalent
            # Risk 1% of equity = ₹1,000. Lot size 65
            opt_risk_per_share = active_trade["risk_pts"] * 0.50
            lots = max(1, int(1000.0 / (opt_risk_per_share * lot_size)))
            qty = lots * lot_size
            trade_pnl = round(opt_pts * qty, 2)

            active_trade.update({
                "exit": round(exit_p, 1),
                "exit_time": exit_time,
                "outcome": outcome,
                "spot_pts": round(pts, 1),
                "opt_pnl_inr": trade_pnl,
                "lots": lots
            })
            trades.append(active_trade)

    # Compile Summary
    df_trades = pd.DataFrame(trades)
    if len(df_trades) == 0:
        print("No trades triggered.")
        return {}

    wins = df_trades[df_trades["outcome"].str.contains("TARGET")]
    losses = df_trades[df_trades["outcome"].str.contains("STOP")]
    eod = df_trades[df_trades["outcome"].str.contains("EOD")]

    total_trades = len(df_trades)
    win_rate = (len(wins) / total_trades) * 100.0
    total_pnl = df_trades["opt_pnl_inr"].sum()
    gross_profit = df_trades[df_trades["opt_pnl_inr"] > 0]["opt_pnl_inr"].sum()
    gross_loss = abs(df_trades[df_trades["opt_pnl_inr"] < 0]["opt_pnl_inr"].sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 999.0

    print("\n" + "="*80)
    print("                    STRATEGY II: VOLUME-BACKED ORB PERFORMANCE")
    print("="*80)
    print(f"  Total Calendar Period     : Past 30 Days")
    print(f"  Trading Sessions Analyzed : {len(daily_groups)} Days")
    print(f"  Total Signals Generated   : {total_trades} Trades")
    print(f"  Target Hits (1:2 R:R)     : {len(wins)} ({len(wins)/total_trades*100:.1f}%)")
    print(f"  Stop Loss Hits            : {len(losses)} ({len(losses)/total_trades*100:.1f}%)")
    print(f"  EOD Time Exits            : {len(eod)} ({len(eod)/total_trades*100:.1f}%)")
    print(f"  Win Rate (Strict Target)  : {win_rate:.2f}%")
    print(f"  Profit Factor             : {profit_factor:.2f}")
    print(f"  Total Spot Points Net     : {df_trades['spot_pts'].sum():+.1f} pts")
    print(f"  Theoretical Option PnL    : INR {total_pnl:+,.2f} on INR 1,00,000 capital ({total_pnl/account_equity*100:+.2f}%)")
    print("="*80)

    print("\nTRADE LOG (PAST 1 MONTH):")
    print("-" * 90)
    print(f"{'Date':<11} {'Time':<6} {'Type':<8} {'Entry':<9} {'SL':<9} {'Target':<9} {'Exit':<9} {'Spot Pts':<10} {'Outcome':<18} {'PnL (INR)':<10}")
    print("-" * 90)
    for _, t in df_trades.iterrows():
        print(f"{t['date']:<11} {t['time']:<6} {t['direction']:<8} {t['entry']:<9} {t['sl']:<9} {t['tgt']:<9} {t['exit']:<9} {t['spot_pts']:<10} {t['outcome']:<18} {t['opt_pnl_inr']:<10}")
    print("-" * 90)

    return {
        "total_trades": total_trades,
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 2),
        "total_pnl": round(total_pnl, 2),
        "trades": trades
    }

def run_vwap_ema_backtest_1month() -> Dict[str, Any]:
    print("\n" + "="*80)
    print("      STRATEGY III: VWAP & EMA INSTITUTIONAL ALIGNMENT (PAST 1 MONTH)")
    print("="*80)

    df = yf.download('^NSEI', period='1mo', interval='5m', progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0].lower() for col in df.columns]
    else:
        df.columns = [col.lower() for col in df.columns]

    df = df.dropna()

    # Indicators
    df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()

    # Cumulative VWAP per session
    df['date'] = df.index.date
    # Typical price
    df['typical_p'] = (df['high'] + df['low'] + df['close']) / 3.0
    df['volume_adj'] = df['volume'].replace(0, 1)
    df['pv'] = df['typical_p'] * df['volume_adj']
    df['cum_pv'] = df.groupby('date')['pv'].cumsum()
    df['cum_vol'] = df.groupby('date')['volume_adj'].cumsum()
    df['vwap'] = df['cum_pv'] / df['cum_vol']

    # Wilder RSI 14
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / (avg_loss.replace(0, 1e-6))
    df['rsi14'] = 100 - (100 / (1 + rs))

    trades = []
    daily_groups = df.groupby('date')
    account_equity = 100000.0
    lot_size = 65

    for trade_date, day_df in daily_groups:
        day_df_ist = day_df.tz_convert('Asia/Kolkata') if day_df.index.tz is not None else day_df
        scan_window = day_df_ist.between_time('09:20', '10:30')
        active_trade = None

        for ts, bar in scan_window.iterrows():
            if active_trade:
                break

            o, h, l, c = float(bar['open']), float(bar['high']), float(bar['low']), float(bar['close'])
            ema9_val, ema21_val = float(bar['ema9']), float(bar['ema21'])
            vwap_val = float(bar['vwap'])
            rsi_val = float(bar['rsi14'])
            rng = max(h - l, 1e-5)

            # CE Bullish Pullback
            if (ema9_val > ema21_val) and (c > vwap_val):
                tested_ema9 = (l <= ema9_val * 1.0005 and c > ema9_val)
                tested_vwap = (abs(l - vwap_val) <= (0.0005 * c))
                bottom_wick = (min(o, c) - l) / rng

                if (tested_ema9 or tested_vwap) and (bottom_wick >= 0.40) and (52.0 <= rsi_val <= 68.0):
                    sl_p = min(l, ema9_val * 0.999)
                    risk_pts = c - sl_p
                    if risk_pts < 10.0 or risk_pts > 80.0:
                        continue
                    tgt_p = c + (2.0 * risk_pts)

                    active_trade = {
                        "date": trade_date.strftime('%Y-%m-%d'),
                        "time": ts.strftime('%H:%M'),
                        "direction": "CE_PULLBACK",
                        "entry": round(c, 1),
                        "sl": round(sl_p, 1),
                        "tgt": round(tgt_p, 1),
                        "risk_pts": round(risk_pts, 1),
                        "entry_ts": ts
                    }

            # PE Bearish Pullback
            elif (ema9_val < ema21_val) and (c < vwap_val):
                tested_ema9 = (h >= ema9_val * 0.9995 and c < ema9_val)
                tested_vwap = (abs(h - vwap_val) <= (0.0005 * c))
                top_wick = (h - max(o, c)) / rng

                if (tested_ema9 or tested_vwap) and (top_wick >= 0.40) and (32.0 <= rsi_val <= 48.0):
                    sl_p = max(h, ema9_val * 1.001)
                    risk_pts = sl_p - c
                    if risk_pts < 10.0 or risk_pts > 80.0:
                        continue
                    tgt_p = c - (2.0 * risk_pts)

                    active_trade = {
                        "date": trade_date.strftime('%Y-%m-%d'),
                        "time": ts.strftime('%H:%M'),
                        "direction": "PE_PULLBACK",
                        "entry": round(c, 1),
                        "sl": round(sl_p, 1),
                        "tgt": round(tgt_p, 1),
                        "risk_pts": round(risk_pts, 1),
                        "entry_ts": ts
                    }

        if active_trade:
            entry_ts = active_trade["entry_ts"]
            subsequent_bars = day_df_ist.loc[entry_ts:].iloc[1:]
            outcome = "EOD_EXIT"
            exit_p = float(subsequent_bars.iloc[-1]['close']) if len(subsequent_bars) > 0 else active_trade["entry"]

            for post_ts, pbar in subsequent_bars.iterrows():
                high_p = float(pbar['high'])
                low_p = float(pbar['low'])

                if active_trade["direction"] == "CE_PULLBACK":
                    if high_p >= active_trade["tgt"]:
                        outcome = "TARGET_HIT (1:2)"
                        exit_p = active_trade["tgt"]
                        break
                    elif low_p <= active_trade["sl"]:
                        outcome = "STOP_HIT"
                        exit_p = active_trade["sl"]
                        break
                else:
                    if low_p <= active_trade["tgt"]:
                        outcome = "TARGET_HIT (1:2)"
                        exit_p = active_trade["tgt"]
                        break
                    elif high_p >= active_trade["sl"]:
                        outcome = "STOP_HIT"
                        exit_p = active_trade["sl"]
                        break

            if active_trade["direction"] == "CE_PULLBACK":
                pts = exit_p - active_trade["entry"]
            else:
                pts = active_trade["entry"] - exit_p

            opt_pts = pts * 0.50
            opt_risk_per_share = active_trade["risk_pts"] * 0.50
            lots = max(1, int(1000.0 / (opt_risk_per_share * lot_size)))
            qty = lots * lot_size
            trade_pnl = round(opt_pts * qty, 2)

            active_trade.update({
                "exit": round(exit_p, 1),
                "outcome": outcome,
                "spot_pts": round(pts, 1),
                "opt_pnl_inr": trade_pnl
            })
            trades.append(active_trade)

    df_t = pd.DataFrame(trades)
    if len(df_t) == 0:
        print("No trades triggered.")
        return {}

    wins = df_t[df_t["outcome"].str.contains("TARGET")]
    losses = df_t[df_t["outcome"].str.contains("STOP")]
    eod = df_t[df_t["outcome"].str.contains("EOD")]

    total_trades = len(df_t)
    win_rate = (len(wins) / total_trades) * 100.0
    total_pnl = df_t["opt_pnl_inr"].sum()
    gross_profit = df_t[df_t["opt_pnl_inr"] > 0]["opt_pnl_inr"].sum()
    gross_loss = abs(df_t[df_t["opt_pnl_inr"] < 0]["opt_pnl_inr"].sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 999.0

    print(f"  Total Calendar Period     : Past 30 Days")
    print(f"  Trading Sessions Analyzed : {len(daily_groups)} Days")
    print(f"  Total Signals Generated   : {total_trades} Trades")
    print(f"  Target Hits (1:2 R:R)     : {len(wins)} ({len(wins)/total_trades*100:.1f}%)")
    print(f"  Stop Loss Hits            : {len(losses)} ({len(losses)/total_trades*100:.1f}%)")
    print(f"  EOD Time Exits            : {len(eod)} ({len(eod)/total_trades*100:.1f}%)")
    print(f"  Win Rate (Strict Target)  : {win_rate:.2f}%")
    print(f"  Profit Factor             : {profit_factor:.2f}")
    print(f"  Total Spot Points Net     : {df_t['spot_pts'].sum():+.1f} pts")
    print(f"  Theoretical Option PnL    : INR {total_pnl:+,.2f} on INR 1,00,000 capital ({total_pnl/account_equity*100:+.2f}%)")
    print("="*80)

    print("\nTRADE LOG (PAST 1 MONTH):")
    print("-" * 90)
    print(f"{'Date':<11} {'Time':<6} {'Type':<12} {'Entry':<9} {'SL':<9} {'Target':<9} {'Exit':<9} {'Spot Pts':<10} {'Outcome':<18} {'PnL (INR)':<10}")
    print("-" * 90)
    for _, t in df_t.iterrows():
        print(f"{t['date']:<11} {t['time']:<6} {t['direction']:<12} {t['entry']:<9} {t['sl']:<9} {t['tgt']:<9} {t['exit']:<9} {t['spot_pts']:<10} {t['outcome']:<18} {t['opt_pnl_inr']:<10}")
    print("-" * 90)

    return {
        "total_trades": total_trades,
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 2),
        "total_pnl": round(total_pnl, 2)
    }

if __name__ == "__main__":
    run_orb_backtest_1month()
    run_vwap_ema_backtest_1month()
