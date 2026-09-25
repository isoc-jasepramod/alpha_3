"""
PROJECT ALPHA 2.0 - All 4 Strategies Backtest (Real NIFTY 50 Data, 1 Month)
UPDATED WITH CONFIDENCE SCORING & ADAPTIVE RISK FILTERS
"""
import sys, os
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, time, timedelta, date
from typing import Dict, Any, List

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

ACCOUNT_EQUITY = 100000.0
LOT_SIZE = 65
RISK_PCT = 0.01
DELTA_ATM = 0.50
CONFIDENCE_THRESHOLD = 60

def fetch_nifty_data(period='1mo', interval='5m'):
    df = yf.download('^NSEI', period=period, interval=interval, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0].lower() for col in df.columns]
    else:
        df.columns = [col.lower() for col in df.columns]
    df = df.dropna()
    return df

def compute_adx(df, period=14):
    high = df['high']
    low = df['low']
    close = df['close']
    prev_close = close.shift(1)
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    up_move = high - prev_high
    down_move = prev_low - low
    
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    
    tr_smooth = pd.Series(tr).ewm(alpha=1/period, adjust=False).mean()
    plus_dm_smooth = pd.Series(plus_dm, index=df.index).ewm(alpha=1/period, adjust=False).mean()
    minus_dm_smooth = pd.Series(minus_dm, index=df.index).ewm(alpha=1/period, adjust=False).mean()
    
    plus_di = 100 * (plus_dm_smooth / tr_smooth.replace(0, 1e-6))
    minus_di = 100 * (minus_dm_smooth / tr_smooth.replace(0, 1e-6))
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-6))
    adx = dx.ewm(alpha=1/period, adjust=False).mean()
    return adx

def compute_confidence(conditions: dict) -> int:
    score = 0
    if conditions.get("trend_alignment"): score += 20
    if conditions.get("volume_confirmation"): score += 25
    score += min(20, max(0, int(conditions.get("momentum_strength", 0.0) * 20)))
    if conditions.get("time_quality"): score += 15
    if conditions.get("context_filter"): score += 20
    return min(100, score)

def resolve_trade(active_trade, subsequent_bars, direction_key='CE', partial_profit_1r=True):
    is_long = direction_key in ('CE', 'CE_LONG', 'CE_PULLBACK', 'CE_BREAKOUT', 'CE_SQUEEZE')
    entry_p = active_trade["entry"]
    risk = active_trade["risk_pts"]
    tgt_2r = active_trade["tgt"]
    tgt_1r = (entry_p + risk) if is_long else (entry_p - risk)
    current_sl = active_trade["sl"]

    opt_risk_per_share = risk * DELTA_ATM
    lots = max(1, int((ACCOUNT_EQUITY * RISK_PCT) / (opt_risk_per_share * LOT_SIZE))) if opt_risk_per_share > 0 else 1
    total_qty = lots * LOT_SIZE
    qty_t1 = total_qty // 2
    qty_t2 = total_qty - qty_t1

    t1_exited = False
    t1_exit_p = entry_p
    t1_outcome = "PENDING"
    t1_time = "15:15"

    t2_exited = False
    t2_exit_p = entry_p
    t2_outcome = "PENDING"
    t2_time = "15:15"

    for post_ts, pbar in subsequent_bars.iterrows():
        h = float(pbar['high'])
        l = float(pbar['low'])
        ts_str = post_ts.strftime('%H:%M')

        if is_long:
            # Case 1: T1 has not exited yet
            if not t1_exited:
                # Check if bar hits stop loss first
                if l <= current_sl:
                    t1_exited = True
                    t1_exit_p = current_sl
                    t1_outcome = "STOP_HIT"
                    t1_time = ts_str
                    t2_exited = True
                    t2_exit_p = current_sl
                    t2_outcome = "STOP_HIT"
                    t2_time = ts_str
                    break

                # Check if bar hits Target 1 (+1R)
                if h >= tgt_1r:
                    t1_exited = True
                    t1_exit_p = tgt_1r
                    t1_outcome = "T1_HIT"
                    t1_time = ts_str
                    # Move stop loss on Tranche 2 to Entry (Breakeven)
                    current_sl = entry_p

                    # Check if bar also hit Target 2 (+2R) in the same candle
                    if h >= tgt_2r:
                        t2_exited = True
                        t2_exit_p = tgt_2r
                        t2_outcome = "T2_HIT"
                        t2_time = ts_str
                        break

            # Case 2: T1 already exited at +1R, managing T2 at Breakeven
            else:
                if h >= tgt_2r:
                    t2_exited = True
                    t2_exit_p = tgt_2r
                    t2_outcome = "T2_HIT"
                    t2_time = ts_str
                    break
                elif l <= current_sl: # Hit Breakeven SL
                    t2_exited = True
                    t2_exit_p = current_sl
                    t2_outcome = "BE_SCRATCH"
                    t2_time = ts_str
                    break
        else: # PE (Short)
            if not t1_exited:
                if h >= current_sl:
                    t1_exited = True
                    t1_exit_p = current_sl
                    t1_outcome = "STOP_HIT"
                    t1_time = ts_str
                    t2_exited = True
                    t2_exit_p = current_sl
                    t2_outcome = "STOP_HIT"
                    t2_time = ts_str
                    break

                if l <= tgt_1r:
                    t1_exited = True
                    t1_exit_p = tgt_1r
                    t1_outcome = "T1_HIT"
                    t1_time = ts_str
                    current_sl = entry_p

                    if l <= tgt_2r:
                        t2_exited = True
                        t2_exit_p = tgt_2r
                        t2_outcome = "T2_HIT"
                        t2_time = ts_str
                        break
            else:
                if l <= tgt_2r:
                    t2_exited = True
                    t2_exit_p = tgt_2r
                    t2_outcome = "T2_HIT"
                    t2_time = ts_str
                    break
                elif h >= current_sl:
                    t2_exited = True
                    t2_exit_p = current_sl
                    t2_outcome = "BE_SCRATCH"
                    t2_time = ts_str
                    break

    # EOD Exit handling for unexited tranches
    last_close = float(subsequent_bars.iloc[-1]['close']) if len(subsequent_bars) > 0 else entry_p
    if not t1_exited:
        t1_exited = True
        t1_exit_p = last_close
        t1_outcome = "EOD_EXIT"
    if not t2_exited:
        t2_exited = True
        t2_exit_p = last_close
        t2_outcome = "EOD_EXIT"

    # Compute PnL per tranche
    pts_t1 = (t1_exit_p - entry_p) if is_long else (entry_p - t1_exit_p)
    pts_t2 = (t2_exit_p - entry_p) if is_long else (entry_p - t2_exit_p)

    pnl_t1 = round(pts_t1 * DELTA_ATM * qty_t1, 2)
    pnl_t2 = round(pts_t2 * DELTA_ATM * qty_t2, 2)
    total_pnl = round(pnl_t1 + pnl_t2, 2)

    avg_exit_p = round((t1_exit_p * qty_t1 + t2_exit_p * qty_t2) / total_qty, 1)
    net_pts = round((avg_exit_p - entry_p) if is_long else (entry_p - avg_exit_p), 1)

    # Classify overall trade outcome
    if t1_outcome == "T1_HIT" and t2_outcome == "T2_HIT":
        overall_outcome = "TARGET_HIT"
    elif t1_outcome == "T1_HIT" and t2_outcome in ("BE_SCRATCH", "EOD_EXIT"):
        overall_outcome = "PARTIAL_WIN_BE"
    elif t1_outcome == "STOP_HIT":
        overall_outcome = "STOP_HIT"
    else:
        overall_outcome = "EOD_EXIT"

    active_trade.update({
        "tgt_1r": round(tgt_1r, 1),
        "exit": avg_exit_p,
        "exit_time": t2_time if t2_outcome != "PENDING" else t1_time,
        "outcome": overall_outcome,
        "spot_pts": net_pts,
        "opt_pnl_inr": total_pnl,
        "lots": lots,
        "qty": total_qty,
        "tranche_details": f"T1({t1_outcome}: ₹{pnl_t1:+,.0f}) | T2({t2_outcome}: ₹{pnl_t2:+,.0f})"
    })
    return active_trade

def print_strategy_summary(name, trades, daily_count):
    df_t = pd.DataFrame(trades)
    if len(df_t) == 0:
        print(f"\n{'='*80}")
        print(f"  {name}")
        print(f"{'='*80}")
        print(f"  No trades triggered in the period (all candidate signals below confidence threshold or filtered).")
        return {"total_trades": 0, "win_rate": 0, "profit_factor": 0, "total_pnl": 0, "trades": []}

    full_wins = df_t[df_t["outcome"] == "TARGET_HIT"]
    partial_wins = df_t[df_t["outcome"] == "PARTIAL_WIN_BE"]
    all_profitable = df_t[df_t["opt_pnl_inr"] > 0]
    losses = df_t[df_t["outcome"] == "STOP_HIT"]
    scratches = df_t[df_t["outcome"] == "BE_SCRATCH"]
    eod = df_t[df_t["outcome"] == "EOD_EXIT"]
    total = len(df_t)
    win_rate = (len(all_profitable) / total) * 100.0 if total > 0 else 0.0
    total_pnl = df_t["opt_pnl_inr"].sum()
    gross_profit = df_t[df_t["opt_pnl_inr"] > 0]["opt_pnl_inr"].sum()
    gross_loss = abs(df_t[df_t["opt_pnl_inr"] < 0]["opt_pnl_inr"].sum())
    pf = (gross_profit / gross_loss) if gross_loss > 0 else 999.0
    avg_win = all_profitable["opt_pnl_inr"].mean() if len(all_profitable) > 0 else 0
    avg_loss = losses["opt_pnl_inr"].mean() if len(losses) > 0 else 0
    max_dd = df_t["opt_pnl_inr"].cumsum().cummax() - df_t["opt_pnl_inr"].cumsum()
    max_drawdown = max_dd.max() if len(max_dd) > 0 else 0

    print(f"\n{'='*80}")
    print(f"  {name}")
    print(f"{'='*80}")
    print(f"  Trading Sessions Scanned  : {daily_count} Days")
    print(f"  Total Signals Generated   : {total}")
    print(f"  Full Target (+2R) Hits    : {len(full_wins)} ({len(full_wins)/total*100:.1f}%)")
    print(f"  Partial Target (+1R) Wins : {len(partial_wins)} ({len(partial_wins)/total*100:.1f}%)")
    print(f"  Stop Loss Hits (Losses)   : {len(losses)} ({len(losses)/total*100:.1f}%)")
    print(f"  Breakeven Scratches       : {len(scratches)} ({len(scratches)/total*100:.1f}%)")
    print(f"  EOD Time Exits            : {len(eod)} ({len(eod)/total*100:.1f}%)")
    print(f"  TOTAL PROFITABLE TRADES   : {len(all_profitable)} / {total} ({win_rate:.1f}%)")
    print(f"  Profit Factor             : {pf:.2f}")
    print(f"  Avg Win                   : INR {avg_win:+,.0f}")
    print(f"  Avg Loss                  : INR {avg_loss:+,.0f}")
    print(f"  Max Drawdown              : INR {max_drawdown:,.0f}")
    print(f"  Net Spot Points           : {df_t['spot_pts'].sum():+.1f} pts")
    print(f"  NET PNL (Option Equiv.)   : INR {total_pnl:+,.0f} ({total_pnl/ACCOUNT_EQUITY*100:+.2f}%)")
    print(f"{'='*80}")
    print(f"\n  {'Date':<11} {'Time':<6} {'Dir':<12} {'Entry':<8} {'SL':<8} {'Tgt':<8} {'Exit':<8} {'Pts':<8} {'Outcome':<16} {'Conf':<6} {'PnL':>10}")
    print(f"  {'-'*104}")
    for _, t in df_t.iterrows():
        pnl_str = f"INR {t['opt_pnl_inr']:+,.0f}"
        conf_str = f"{t.get('confidence', 75)}%"
        print(f"  {t['date']:<11} {t['time']:<6} {t['direction']:<12} {t['entry']:<8} {t['sl']:<8} {t['tgt']:<8} {t['exit']:<8} {t['spot_pts']:<8} {t['outcome']:<16} {conf_str:<6} {pnl_str:>10}")
    return {
        "total_trades": total,
        "win_rate": round(win_rate, 1),
        "profit_factor": round(pf, 2),
        "total_pnl": round(total_pnl, 2),
        "max_drawdown": round(max_drawdown, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "trades": trades
    }

def run_oi_squeeze_proxy(df):
    """
    Upgraded OI Squeeze with:
    - Loosened RoC (0.10% vs 0.15%)
    - ADX(14) >= 20.0 trend filter
    - Volume ratio >= 1.80x
    - Confidence scoring (>=60)
    """
    df = df.copy()
    df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
    df['vol_sma20'] = df['volume'].rolling(20).mean()
    df['roc3'] = df['close'].pct_change(3) * 100
    df['adx'] = compute_adx(df, 14)
    df['date'] = df.index.date
    trades = []
    daily_groups = df.groupby('date')
    for trade_date, day_df in daily_groups:
        day_ist = day_df.tz_convert('Asia/Kolkata') if day_df.index.tz else day_df
        scan = day_ist.between_time('09:30', '15:15')
        active_trade = None
        for ts, bar in scan.iterrows():
            if active_trade:
                break
            c = float(bar['close'])
            v = float(bar['volume'])
            e9 = float(bar['ema9'])
            e21 = float(bar['ema21'])
            vol_avg = float(bar['vol_sma20']) if not pd.isna(bar['vol_sma20']) else 0
            roc = float(bar['roc3']) if not pd.isna(bar['roc3']) else 0
            adx_val = float(bar['adx']) if not pd.isna(bar['adx']) else 20.0
            if vol_avg <= 0 or v <= 0:
                continue
            vol_ratio = v / vol_avg

            # ADX trend filter: market must not be dead choppy
            if adx_val < 20.0:
                continue

            if vol_ratio >= 1.80 and roc >= 0.10 and e9 > e21 and c > e9:
                conditions = {
                    "trend_alignment": True,
                    "volume_confirmation": vol_ratio >= 2.0,
                    "momentum_strength": min(1.0, roc / 0.25),
                    "time_quality": True,
                    "context_filter": adx_val >= 25.0
                }
                conf = compute_confidence(conditions)
                if conf < CONFIDENCE_THRESHOLD:
                    continue

                risk_pts = max(c - e9, c * 0.0025)
                if risk_pts < 8 or risk_pts > 80:
                    continue
                sl_p = c - risk_pts
                tgt_p = c + (2.0 * risk_pts)
                active_trade = {
                    "date": trade_date.strftime('%Y-%m-%d'),
                    "time": ts.strftime('%H:%M'),
                    "direction": "CE_SQUEEZE",
                    "entry": round(c, 1),
                    "sl": round(sl_p, 1),
                    "tgt": round(tgt_p, 1),
                    "risk_pts": round(risk_pts, 1),
                    "confidence": conf,
                    "entry_ts": ts
                }
            elif vol_ratio >= 1.80 and roc <= -0.10 and e9 < e21 and c < e9:
                conditions = {
                    "trend_alignment": True,
                    "volume_confirmation": vol_ratio >= 2.0,
                    "momentum_strength": min(1.0, abs(roc) / 0.25),
                    "time_quality": True,
                    "context_filter": adx_val >= 25.0
                }
                conf = compute_confidence(conditions)
                if conf < CONFIDENCE_THRESHOLD:
                    continue

                risk_pts = max(e9 - c, c * 0.0025)
                if risk_pts < 8 or risk_pts > 80:
                    continue
                sl_p = c + risk_pts
                tgt_p = c - (2.0 * risk_pts)
                active_trade = {
                    "date": trade_date.strftime('%Y-%m-%d'),
                    "time": ts.strftime('%H:%M'),
                    "direction": "PE_SQUEEZE",
                    "entry": round(c, 1),
                    "sl": round(sl_p, 1),
                    "tgt": round(tgt_p, 1),
                    "risk_pts": round(risk_pts, 1),
                    "confidence": conf,
                    "entry_ts": ts
                }
        if active_trade:
            day_ist_full = day_df.tz_convert('Asia/Kolkata') if day_df.index.tz else day_df
            subsequent = day_ist_full.loc[active_trade["entry_ts"]:].iloc[1:]
            trade = resolve_trade(active_trade, subsequent, active_trade["direction"], partial_profit_1r=True)
            trades.append(trade)
    return trades, len(daily_groups)

def run_orb_backtest(df):
    """
    Upgraded Volume-Backed ORB with:
    - Opening Gap Filter: Suppress breakout if Opening Gap > 0.40%
    - ORB range filter: 30 <= range <= 120 pts
    - Adaptive SL: Entry - (0.5 * range) instead of L_ORB
    - Breakout strength filter: close > (H_ORB + delta) * 1.0005
    - RVOL: 1.50+
    - 50% partial profit at +1R, trail remaining to Breakeven
    - Confidence scoring (>=60)
    """
    df = df.copy()
    df['vol_sma20'] = df['volume'].rolling(20).mean()
    df['date'] = df.index.date
    trades = []
    daily_groups = df.groupby('date')
    prev_close = None

    for trade_date, day_df in daily_groups:
        day_ist = day_df.tz_convert('Asia/Kolkata') if day_df.index.tz else day_df
        orb_win = day_ist.between_time('09:15', '09:25')
        if len(orb_win) < 3:
            prev_close = float(day_df.iloc[-1]['close'])
            continue

        # Opening Gap Filter: suppress if gap > 0.40%
        day_open = float(orb_win.iloc[0]['open'])
        if prev_close is not None and prev_close > 0:
            gap_pct = abs(day_open - prev_close) / prev_close * 100.0
            if gap_pct > 0.40:
                # Exhaustion gap day: skip ORB breakouts
                prev_close = float(day_df.iloc[-1]['close'])
                continue

        prev_close = float(day_df.iloc[-1]['close'])

        h_orb = float(orb_win['high'].max())
        l_orb = float(orb_win['low'].min())
        rng = h_orb - l_orb

        # 1. Range quality filter: 30 <= range <= 120
        if rng < 30.0 or rng > 120.0:
            continue

        delta = h_orb * 0.0003
        strength_buf = 0.0005 # 0.05% breakout strength requirement
        scan = day_ist.between_time('09:30', '10:30')
        active_trade = None
        for ts, bar in scan.iterrows():
            c = float(bar['close'])
            o = float(bar['open'])
            v = float(bar['volume'])
            vol_avg = float(bar['vol_sma20']) if not pd.isna(bar['vol_sma20']) else 0
            rvol = (v / vol_avg) if vol_avg > 0 else 1.6

            # Check bullish breakout with strength filter & RVOL
            if c > ((h_orb + delta) * (1.0 + strength_buf)) and rvol >= 1.50:
                # Adaptive SL: 0.5 * range
                risk_pts = rng * 0.5
                sl_p = c - risk_pts
                tgt_p = c + (2.0 * risk_pts)

                conditions = {
                    "trend_alignment": c > o,
                    "volume_confirmation": rvol >= 1.70,
                    "momentum_strength": min(1.0, (c - h_orb) / max(10.0, rng * 0.25)),
                    "time_quality": ts.time() <= time(10, 15),
                    "context_filter": (40.0 <= rng <= 90.0)
                }
                conf = compute_confidence(conditions)
                if conf < CONFIDENCE_THRESHOLD:
                    continue

                active_trade = {
                    "date": trade_date.strftime('%Y-%m-%d'),
                    "time": ts.strftime('%H:%M'),
                    "direction": "CE_LONG",
                    "entry": round(c, 1),
                    "sl": round(sl_p, 1),
                    "tgt": round(tgt_p, 1),
                    "risk_pts": round(risk_pts, 1),
                    "confidence": conf,
                    "entry_ts": ts
                }
                break

            # Check bearish breakdown with strength filter & RVOL
            elif c < ((l_orb - delta) * (1.0 - strength_buf)) and rvol >= 1.50:
                # Adaptive SL: 0.5 * range
                risk_pts = rng * 0.5
                sl_p = c + risk_pts
                tgt_p = c - (2.0 * risk_pts)

                conditions = {
                    "trend_alignment": c < o,
                    "volume_confirmation": rvol >= 1.70,
                    "momentum_strength": min(1.0, (l_orb - c) / max(10.0, rng * 0.25)),
                    "time_quality": ts.time() <= time(10, 15),
                    "context_filter": (40.0 <= rng <= 90.0)
                }
                conf = compute_confidence(conditions)
                if conf < CONFIDENCE_THRESHOLD:
                    continue

                active_trade = {
                    "date": trade_date.strftime('%Y-%m-%d'),
                    "time": ts.strftime('%H:%M'),
                    "direction": "PE_LONG",
                    "entry": round(c, 1),
                    "sl": round(sl_p, 1),
                    "tgt": round(tgt_p, 1),
                    "risk_pts": round(risk_pts, 1),
                    "confidence": conf,
                    "entry_ts": ts
                }
                break

        if active_trade:
            subsequent = day_ist.loc[active_trade["entry_ts"]:].iloc[1:]
            trade = resolve_trade(active_trade, subsequent, active_trade["direction"], partial_profit_1r=True)
            trades.append(trade)
    return trades, len(daily_groups)

def run_vwap_ema_backtest(df):
    """
    Upgraded VWAP/EMA Alignment with:
    - ADX Regime Filter: ADX >= 22.0 (eliminates chop oscillation)
    - VWAP Slope Filter: Slope >= +0.35 for CE, <= -0.35 for PE
    - Confirmation candle logic (next bar must close above EMA9 for CE / below EMA9 for PE)
    - Widened RSI bands: CE [48, 72], PE [28, 52]
    - Volume confirmation: >= 1.2x avg
    - Tighter structural stop loss
    - 50% partial profit at +1R, trail remaining to Breakeven
    - Confidence scoring (>=60)
    """
    df = df.copy()
    df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
    df['vol_sma20'] = df['volume'].rolling(20).mean()
    df['adx'] = compute_adx(df, 14)
    df['date'] = df.index.date
    df['typical_p'] = (df['high'] + df['low'] + df['close']) / 3.0
    df['volume_adj'] = df['volume'].replace(0, 1)
    df['pv'] = df['typical_p'] * df['volume_adj']
    df['cum_pv'] = df.groupby('date')['pv'].cumsum()
    df['cum_vol'] = df.groupby('date')['volume_adj'].cumsum()
    df['vwap'] = df['cum_pv'] / df['cum_vol']
    df['vwap_slope'] = (df['vwap'] - df['vwap'].shift(4)) / 4.0

    delta_c = df['close'].diff()
    gain = delta_c.clip(lower=0)
    loss_s = -delta_c.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss_s.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / (avg_loss.replace(0, 1e-6))
    df['rsi14'] = 100 - (100 / (1 + rs))

    trades = []
    daily_groups = df.groupby('date')
    for trade_date, day_df in daily_groups:
        day_ist = day_df.tz_convert('Asia/Kolkata') if day_df.index.tz else day_df
        scan = day_ist.between_time('09:20', '10:30')
        active_trade = None
        bars = list(scan.iterrows())

        for idx in range(len(bars) - 1):
            if active_trade:
                break
            ts, bar = bars[idx]
            o, h, l, c = float(bar['open']), float(bar['high']), float(bar['low']), float(bar['close'])
            e9, e21 = float(bar['ema9']), float(bar['ema21'])
            vwap_val = float(bar['vwap'])
            rsi_val = float(bar['rsi14'])
            v = float(bar['volume'])
            vol_avg = float(bar['vol_sma20']) if not pd.isna(bar['vol_sma20']) else 0
            adx_val = float(bar['adx']) if not pd.isna(bar['adx']) else 20.0
            vwap_slope = float(bar['vwap_slope']) if not pd.isna(bar['vwap_slope']) else 0.0
            rng = max(h - l, 1e-5)

            # ADX Regime Gate: eliminate low momentum sideways churn
            if adx_val < 22.0:
                continue

            # Volume filter on rejection candle
            if vol_avg > 0 and (v / vol_avg) < 1.20:
                continue

            # CE Rejection candidate: requires upward sloping VWAP
            if (e9 > e21) and (c > vwap_val) and (vwap_slope >= 0.35):
                tested_ema9 = (l <= e9 * 1.0005 and c > e9)
                tested_vwap = (abs(l - vwap_val) <= (0.0005 * c))
                bottom_wick = (min(o, c) - l) / rng
                if (tested_ema9 or tested_vwap) and (bottom_wick >= 0.40) and (48.0 <= rsi_val <= 72.0):
                    # Check next confirmation bar!
                    next_ts, next_bar = bars[idx + 1]
                    nc_close = float(next_bar['close'])
                    nc_open = float(next_bar['open'])
                    nc_low = float(next_bar['low'])
                    nc_e9 = float(next_bar['ema9'])

                    if nc_close > nc_e9 and nc_close >= nc_open:
                        # Rejection confirmed!
                        sl_p = min(l, nc_low, nc_e9) - 5.0
                        risk_pts = nc_close - sl_p
                        if risk_pts < 8.0 or risk_pts > 60.0:
                            continue
                        tgt_p = nc_close + (2.0 * risk_pts)

                        conditions = {
                            "trend_alignment": True,
                            "volume_confirmation": True,
                            "momentum_strength": min(1.0, (nc_close - nc_open) / max(5.0, (float(next_bar['high']) - nc_low))),
                            "time_quality": next_ts.time() <= time(10, 15),
                            "context_filter": (bottom_wick >= 0.45)
                        }
                        conf = compute_confidence(conditions)
                        if conf < CONFIDENCE_THRESHOLD:
                            continue

                        active_trade = {
                            "date": trade_date.strftime('%Y-%m-%d'),
                            "time": next_ts.strftime('%H:%M'),
                            "direction": "CE_PULLBACK",
                            "entry": round(nc_close, 1),
                            "sl": round(sl_p, 1),
                            "tgt": round(tgt_p, 1),
                            "risk_pts": round(risk_pts, 1),
                            "confidence": conf,
                            "entry_ts": next_ts
                        }
                        break

            # PE Rejection candidate: requires downward sloping VWAP
            elif (e9 < e21) and (c < vwap_val) and (vwap_slope <= -0.35):
                tested_ema9 = (h >= e9 * 0.9995 and c < e9)
                tested_vwap = (abs(h - vwap_val) <= (0.0005 * c))
                top_wick = (h - max(o, c)) / rng
                if (tested_ema9 or tested_vwap) and (top_wick >= 0.40) and (28.0 <= rsi_val <= 52.0):
                    # Check next confirmation bar!
                    next_ts, next_bar = bars[idx + 1]
                    nc_close = float(next_bar['close'])
                    nc_open = float(next_bar['open'])
                    nc_high = float(next_bar['high'])
                    nc_e9 = float(next_bar['ema9'])

                    if nc_close < nc_e9 and nc_close <= nc_open:
                        # Rejection confirmed!
                        sl_p = max(h, nc_high, nc_e9) + 5.0
                        risk_pts = sl_p - nc_close
                        if risk_pts < 8.0 or risk_pts > 60.0:
                            continue
                        tgt_p = nc_close - (2.0 * risk_pts)

                        conditions = {
                            "trend_alignment": True,
                            "volume_confirmation": True,
                            "momentum_strength": min(1.0, (nc_open - nc_close) / max(5.0, (nc_high - float(next_bar['low'])))),
                            "time_quality": next_ts.time() <= time(10, 15),
                            "context_filter": (top_wick >= 0.45)
                        }
                        conf = compute_confidence(conditions)
                        if conf < CONFIDENCE_THRESHOLD:
                            continue

                        active_trade = {
                            "date": trade_date.strftime('%Y-%m-%d'),
                            "time": next_ts.strftime('%H:%M'),
                            "direction": "PE_PULLBACK",
                            "entry": round(nc_close, 1),
                            "sl": round(sl_p, 1),
                            "tgt": round(tgt_p, 1),
                            "risk_pts": round(risk_pts, 1),
                            "confidence": conf,
                            "entry_ts": next_ts
                        }
                        break

        if active_trade:
            subsequent = day_ist.loc[active_trade["entry_ts"]:].iloc[1:]
            trade = resolve_trade(active_trade, subsequent, active_trade["direction"], partial_profit_1r=True)
            trades.append(trade)
    return trades, len(daily_groups)

def run_gamma_scalp_backtest(df):
    """
    Upgraded Expiry-Day Gamma Scalp with:
    - Lowered RoC: 0.06% (vs 0.10%)
    - Consolidation quality filter: midday range < 0.5% of spot
    - Adaptive SL: H_mid - 0.2 * risk
    - Breakeven trail after +1R
    - Confidence scoring (>=60)
    """
    df = df.copy()
    df['date'] = df.index.date
    trades = []
    daily_groups = df.groupby('date')
    expiry_weekday = 1  # Tuesday for NIFTY
    for trade_date, day_df in daily_groups:
        if trade_date.weekday() != expiry_weekday:
            continue
        day_ist = day_df.tz_convert('Asia/Kolkata') if day_df.index.tz else day_df
        mid_window = day_ist.between_time('12:00', '13:00')
        if len(mid_window) < 6:
            continue
        h_mid = float(mid_window['high'].max())
        l_mid = float(mid_window['low'].min())
        mid_range = h_mid - l_mid

        gamma_window = day_ist.between_time('13:15', '15:15')
        active_trade = None
        closes = []
        for ts, bar in gamma_window.iterrows():
            c = float(bar['close'])
            closes.append(c)
            if len(closes) < 4:
                continue

            # Consolidation quality: range < 0.5% of spot
            if mid_range > (0.005 * c) or mid_range <= 0:
                continue

            roc_3 = ((closes[-1] - closes[-4]) / closes[-4]) * 100.0

            if c > h_mid and roc_3 >= 0.06:
                risk_pts = max(c - h_mid, c * 0.0018)
                if risk_pts < 5 or risk_pts > 50:
                    continue
                sl_p = h_mid - (risk_pts * 0.2)
                tgt_p = c + (2.0 * risk_pts)

                conditions = {
                    "trend_alignment": roc_3 >= 0.08,
                    "volume_confirmation": True,
                    "momentum_strength": min(1.0, roc_3 / 0.12),
                    "time_quality": ts.time() <= time(15, 0),
                    "context_filter": (mid_range <= (0.0035 * c))
                }
                conf = compute_confidence(conditions)
                if conf < CONFIDENCE_THRESHOLD:
                    continue

                active_trade = {
                    "date": trade_date.strftime('%Y-%m-%d'),
                    "time": ts.strftime('%H:%M'),
                    "direction": "CE_BREAKOUT",
                    "entry": round(c, 1),
                    "sl": round(sl_p, 1),
                    "tgt": round(tgt_p, 1),
                    "risk_pts": round(risk_pts, 1),
                    "confidence": conf,
                    "entry_ts": ts
                }
                break

            elif c < l_mid and roc_3 <= -0.06:
                risk_pts = max(l_mid - c, c * 0.0018)
                if risk_pts < 5 or risk_pts > 50:
                    continue
                sl_p = l_mid + (risk_pts * 0.2)
                tgt_p = c - (2.0 * risk_pts)

                conditions = {
                    "trend_alignment": roc_3 <= -0.08,
                    "volume_confirmation": True,
                    "momentum_strength": min(1.0, abs(roc_3) / 0.12),
                    "time_quality": ts.time() <= time(15, 0),
                    "context_filter": (mid_range <= (0.0035 * c))
                }
                conf = compute_confidence(conditions)
                if conf < CONFIDENCE_THRESHOLD:
                    continue

                active_trade = {
                    "date": trade_date.strftime('%Y-%m-%d'),
                    "time": ts.strftime('%H:%M'),
                    "direction": "PE_BREAKOUT",
                    "entry": round(c, 1),
                    "sl": round(sl_p, 1),
                    "tgt": round(tgt_p, 1),
                    "risk_pts": round(risk_pts, 1),
                    "confidence": conf,
                    "entry_ts": ts
                }
                break

        if active_trade:
            subsequent = day_ist.loc[active_trade["entry_ts"]:].iloc[1:]
            trade = resolve_trade(active_trade, subsequent, active_trade["direction"], partial_profit_1r=True)
            trades.append(trade)
    return trades, len(daily_groups)

if __name__ == "__main__":
    print("\n" + "="*80)
    print("  PROJECT ALPHA 2.0 | RE-OPTIMIZED 4-STRATEGY BACKTEST (REAL NIFTY 50 DATA)")
    print("  Filters: Confidence Scoring (>=60), Adaptive SL, Quality Range, Breakeven Trail")
    print("="*80)
    df = fetch_nifty_data(period='1mo', interval='5m')
    print(f"\n  Loaded {len(df)} candles from {df.index[0].strftime('%Y-%m-%d')} to {df.index[-1].strftime('%Y-%m-%d')}")
    oi_trades, oi_days = run_oi_squeeze_proxy(df)
    orb_trades, orb_days = run_orb_backtest(df)
    vwap_trades, vwap_days = run_vwap_ema_backtest(df)
    gamma_trades, gamma_days = run_gamma_scalp_backtest(df)

    r1 = print_strategy_summary("STRATEGY I: OI SQUEEZE SENTINEL (Volume-Price Divergence + ADX)", oi_trades, oi_days)
    r2 = print_strategy_summary("STRATEGY II: VOLUME-BACKED ORB (Adaptive SL + Range Quality + BE Trail)", orb_trades, orb_days)
    r3 = print_strategy_summary("STRATEGY III: VWAP/EMA INSTITUTIONAL ALIGNMENT (Confirmation Candle + BE Trail)", vwap_trades, vwap_days)
    r4 = print_strategy_summary("STRATEGY IV: EXPIRY-DAY GAMMA SCALP (0.06% RoC + Mid-Range Filter)", gamma_trades, gamma_days)

    print(f"\n\n{'='*80}")
    print(f"  OPTIMIZED COMPARATIVE STRATEGY PERFORMANCE MATRIX")
    print(f"{'='*80}")
    print(f"\n  {'Strategy':<45} {'Trades':>7} {'WinRate':>8} {'PF':>6} {'Net PnL':>12} {'MaxDD':>10} {'AvgWin':>10} {'AvgLoss':>10}")
    print(f"  {'-'*110}")
    for name, r in [("OI Squeeze Sentinel", r1), ("Volume-Backed ORB", r2),
                    ("VWAP/EMA Alignment", r3), ("Expiry Gamma Scalp", r4)]:
        if r["total_trades"] > 0:
            print(f"  {name:<45} {r['total_trades']:>7} {r['win_rate']:>7.1f}% {r['profit_factor']:>5.2f} INR{r['total_pnl']:>+10,.0f} INR{r.get('max_drawdown',0):>8,.0f} INR{r.get('avg_win',0):>8,.0f} INR{r.get('avg_loss',0):>8,.0f}")
        else:
            print(f"  {name:<45} {'--':>7} {'--':>8} {'--':>6} {'--':>12} {'--':>10} {'--':>10} {'--':>10}")
    combined = r1.get('total_pnl',0) + r2.get('total_pnl',0) + r3.get('total_pnl',0) + r4.get('total_pnl',0)
    print(f"\n  Combined Net PnL: INR {combined:+,.0f}")
    print(f"{'='*80}\n")
