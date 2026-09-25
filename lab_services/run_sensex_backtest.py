"""
PROJECT ALPHA 2.0 / 3.0 - BSE SENSEX 1-MONTH BACKTEST VALIDATION
Ticker: ^BSESN (Real 5-minute BSE SENSEX Spot Data)
Validates all strategy engines with SENSEX specifications:
- Spot Scale: ~80,000 pts
- Lot Size: 20 (BSE F&O standard)
- Expiry Day for Gamma Scalp: Friday (weekday 4)
- Range & Stop filters calibrated to SENSEX index dimensions
- Dual Targets (+1.0R partial exit 50%, BE stop trail, +2.0R target)
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
SENSEX_LOT_SIZE = 20
RISK_PCT = 0.01          # 1% risk per trade = INR 1,000
DELTA_ATM = 0.50
CONFIDENCE_THRESHOLD = 60

def fetch_sensex_data(period='1mo', interval='5m'):
    df = yf.download('^BSESN', period=period, interval=interval, progress=False)
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
    lots = max(1, int((ACCOUNT_EQUITY * RISK_PCT) / (opt_risk_per_share * SENSEX_LOT_SIZE))) if opt_risk_per_share > 0 else 1
    total_qty = lots * SENSEX_LOT_SIZE
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
            if not t1_exited:
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

                if h >= tgt_1r:
                    t1_exited = True
                    t1_exit_p = tgt_1r
                    t1_outcome = "T1_HIT"
                    t1_time = ts_str
                    current_sl = entry_p # Move to BE

                    if h >= tgt_2r:
                        t2_exited = True
                        t2_exit_p = tgt_2r
                        t2_outcome = "T2_HIT"
                        t2_time = ts_str
                        break
            else:
                if h >= tgt_2r:
                    t2_exited = True
                    t2_exit_p = tgt_2r
                    t2_outcome = "T2_HIT"
                    t2_time = ts_str
                    break
                elif l <= current_sl:
                    t2_exited = True
                    t2_exit_p = current_sl
                    t2_outcome = "BE_SCRATCH"
                    t2_time = ts_str
                    break
        else: # PE
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

    last_close = float(subsequent_bars.iloc[-1]['close']) if len(subsequent_bars) > 0 else entry_p
    if not t1_exited:
        t1_exited = True
        t1_exit_p = last_close
        t1_outcome = "EOD_EXIT"
    if not t2_exited:
        t2_exited = True
        t2_exit_p = last_close
        t2_outcome = "EOD_EXIT"

    pts_t1 = (t1_exit_p - entry_p) if is_long else (entry_p - t1_exit_p)
    pts_t2 = (t2_exit_p - entry_p) if is_long else (entry_p - t2_exit_p)

    pnl_t1 = round(pts_t1 * DELTA_ATM * qty_t1, 2)
    pnl_t2 = round(pts_t2 * DELTA_ATM * qty_t2, 2)
    total_pnl = round(pnl_t1 + pnl_t2, 2)

    avg_exit_p = round((t1_exit_p * qty_t1 + t2_exit_p * qty_t2) / total_qty, 1)
    net_pts = round((avg_exit_p - entry_p) if is_long else (entry_p - avg_exit_p), 1)

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
        "tranche_details": f"T1({t1_outcome}: INR {pnl_t1:+,.0f}) | T2({t2_outcome}: INR {pnl_t2:+,.0f})"
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

def run_sensex_orb(df):
    """
    Volume-Backed ORB calibrated for BSE SENSEX (~80,000 pts)
    - Range filter: 100 <= range <= 400 pts (0.12% - 0.50% of spot)
    - Opening Gap Filter: > 0.40% gap suppressed
    - Adaptive SL: 0.5 * range
    - Breakout clearance: 0.05%
    - 50% partial profit at +1R, trail to BE
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

        day_open = float(orb_win.iloc[0]['open'])
        if prev_close is not None and prev_close > 0:
            gap_pct = abs(day_open - prev_close) / prev_close * 100.0
            if gap_pct > 0.40:
                prev_close = float(day_df.iloc[-1]['close'])
                continue

        prev_close = float(day_df.iloc[-1]['close'])
        h_orb = float(orb_win['high'].max())
        l_orb = float(orb_win['low'].min())
        rng = h_orb - l_orb

        # SENSEX range scale: 100 to 400 pts
        if rng < 100.0 or rng > 400.0:
            continue

        delta = h_orb * 0.0003
        strength_buf = 0.0005
        scan = day_ist.between_time('09:30', '10:30')
        active_trade = None

        for ts, bar in scan.iterrows():
            c = float(bar['close'])
            o = float(bar['open'])
            v = float(bar['volume'])
            vol_avg = float(bar['vol_sma20']) if not pd.isna(bar['vol_sma20']) else 0
            rvol = (v / vol_avg) if vol_avg > 0 else 1.6

            if c > ((h_orb + delta) * (1.0 + strength_buf)) and rvol >= 1.50:
                risk_pts = rng * 0.5
                sl_p = c - risk_pts
                tgt_p = c + (2.0 * risk_pts)

                conditions = {
                    "trend_alignment": c > o,
                    "volume_confirmation": rvol >= 1.70,
                    "momentum_strength": min(1.0, (c - h_orb) / max(30.0, rng * 0.25)),
                    "time_quality": ts.time() <= time(10, 15),
                    "context_filter": (120.0 <= rng <= 300.0)
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

            elif c < ((l_orb - delta) * (1.0 - strength_buf)) and rvol >= 1.50:
                risk_pts = rng * 0.5
                sl_p = c + risk_pts
                tgt_p = c - (2.0 * risk_pts)

                conditions = {
                    "trend_alignment": c < o,
                    "volume_confirmation": rvol >= 1.70,
                    "momentum_strength": min(1.0, (l_orb - c) / max(30.0, rng * 0.25)),
                    "time_quality": ts.time() <= time(10, 15),
                    "context_filter": (120.0 <= rng <= 300.0)
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

def run_sensex_vwap_ema(df):
    """
    VWAP & EMA Institutional Alignment calibrated for BSE SENSEX
    - ADX >= 22.0
    - SENSEX VWAP Slope: |Slope| >= 1.0 pt/bar (equivalent to 0.35 on Nifty)
    - Confirmation candle logic
    - 50% partial profit at +1R, trail to BE
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

            if adx_val < 22.0:
                continue
            if vol_avg > 0 and (v / vol_avg) < 1.20:
                continue

            # CE Rejection candidate on SENSEX
            if (e9 > e21) and (c > vwap_val) and (vwap_slope >= 1.0):
                tested_ema9 = (l <= e9 * 1.0005 and c > e9)
                tested_vwap = (abs(l - vwap_val) <= (0.0005 * c))
                bottom_wick = (min(o, c) - l) / rng
                if (tested_ema9 or tested_vwap) and (bottom_wick >= 0.40) and (48.0 <= rsi_val <= 72.0):
                    next_ts, next_bar = bars[idx + 1]
                    nc_close = float(next_bar['close'])
                    nc_open = float(next_bar['open'])
                    nc_low = float(next_bar['low'])
                    nc_e9 = float(next_bar['ema9'])

                    if nc_close > nc_e9 and nc_close >= nc_open:
                        sl_p = min(l, nc_low, nc_e9) - 15.0
                        risk_pts = nc_close - sl_p
                        if risk_pts < 25.0 or risk_pts > 200.0:
                            continue
                        tgt_p = nc_close + (2.0 * risk_pts)

                        conditions = {
                            "trend_alignment": True,
                            "volume_confirmation": True,
                            "momentum_strength": min(1.0, (nc_close - nc_open) / max(15.0, (float(next_bar['high']) - nc_low))),
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

            # PE Rejection candidate on SENSEX
            elif (e9 < e21) and (c < vwap_val) and (vwap_slope <= -1.0):
                tested_ema9 = (h >= e9 * 0.9995 and c < e9)
                tested_vwap = (abs(h - vwap_val) <= (0.0005 * c))
                top_wick = (h - max(o, c)) / rng
                if (tested_ema9 or tested_vwap) and (top_wick >= 0.40) and (28.0 <= rsi_val <= 52.0):
                    next_ts, next_bar = bars[idx + 1]
                    nc_close = float(next_bar['close'])
                    nc_open = float(next_bar['open'])
                    nc_high = float(next_bar['high'])
                    nc_e9 = float(next_bar['ema9'])

                    if nc_close < nc_e9 and nc_close <= nc_open:
                        sl_p = max(h, nc_high, nc_e9) + 15.0
                        risk_pts = sl_p - nc_close
                        if risk_pts < 25.0 or risk_pts > 200.0:
                            continue
                        tgt_p = nc_close - (2.0 * risk_pts)

                        conditions = {
                            "trend_alignment": True,
                            "volume_confirmation": True,
                            "momentum_strength": min(1.0, (nc_open - nc_close) / max(15.0, (nc_high - float(next_bar['low'])))),
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

def run_sensex_gamma_scalp(df):
    """
    Expiry-Day Gamma Scalp for BSE SENSEX
    - Expiry Day: FRIDAY (weekday 4)
    - Consolidation window: 12:00 - 13:00
    - Breakout window: 13:15 - 15:15
    - RoC 3m >= 0.06%
    - 50% partial profit at +1R, trail to BE
    """
    df = df.copy()
    df['date'] = df.index.date
    trades = []
    daily_groups = df.groupby('date')
    sensex_expiry_weekday = 4 # FRIDAY

    for trade_date, day_df in daily_groups:
        if trade_date.weekday() != sensex_expiry_weekday:
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

            if mid_range > (0.005 * c) or mid_range <= 0:
                continue

            roc_3 = ((closes[-1] - closes[-4]) / closes[-4]) * 100.0

            if c > h_mid and roc_3 >= 0.06:
                risk_pts = max(c - h_mid, c * 0.0018)
                if risk_pts < 15 or risk_pts > 150:
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
                if risk_pts < 15 or risk_pts > 150:
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
    print("  PROJECT ALPHA 2.0 / 3.0 | BSE SENSEX 1-MONTH QUANTITATIVE BACKTEST")
    print("  Spot Index: BSE SENSEX (^BSESN) | Lot Size: 20 | Risk: 1% (INR 1,000)")
    print("  Dual Targets: +1.0R (50% Partial Exit) & +2.0R (Trail BE)")
    print("="*80)

    df_sensex = fetch_sensex_data(period='1mo', interval='5m')
    print(f"\n  Loaded {len(df_sensex)} candles from {df_sensex.index[0].strftime('%Y-%m-%d')} to {df_sensex.index[-1].strftime('%Y-%m-%d')}")

    print("\n" + "="*80)
    print("  STRATEGY I: VOLUME-BACKED ORB (BSE SENSEX)")
    print("="*80)
    orb_trades, days_count = run_sensex_orb(df_sensex)
    orb_res = print_strategy_summary("Volume-Backed ORB (SENSEX)", orb_trades, days_count)

    print("\n" + "="*80)
    print("  STRATEGY II: VWAP/EMA INSTITUTIONAL ALIGNMENT (BSE SENSEX)")
    print("="*80)
    vwap_trades, days_count = run_sensex_vwap_ema(df_sensex)
    vwap_res = print_strategy_summary("VWAP/EMA Alignment (SENSEX)", vwap_trades, days_count)

    print("\n" + "="*80)
    print("  STRATEGY III: EXPIRY-DAY GAMMA SCALP (BSE SENSEX - FRIDAY EXPIRIES)")
    print("="*80)
    gamma_trades, days_count = run_sensex_gamma_scalp(df_sensex)
    gamma_res = print_strategy_summary("Expiry-Day Gamma Scalp (SENSEX Friday)", gamma_trades, days_count)

    # Performance Matrix
    print("\n\n" + "="*80)
    print("  BSE SENSEX STRATEGY PERFORMANCE MATRIX (PAST 1 MONTH)")
    print("="*80)
    print(f"\n  {'Strategy':<38} {'Trades':>8} {'WinRate':>10} {'PF':>8} {'Net PnL':>14} {'MaxDD':>12}")
    print(f"  {'-'*94}")
    
    total_trades = 0
    total_pnl = 0.0

    for res, label in [(orb_res, "Volume-Backed ORB (SENSEX)"), (vwap_res, "VWAP/EMA Alignment (SENSEX)"), (gamma_res, "Expiry Gamma Scalp (SENSEX Friday)")]:
        if res["total_trades"] > 0:
            total_trades += res["total_trades"]
            total_pnl += res["total_pnl"]
            print(f"  {label:<38} {res['total_trades']:>8} {res['win_rate']:>9.1f}% {res['profit_factor']:>8.2f} INR {res['total_pnl']:>+10,.0f} INR {res['max_drawdown']:>8,.0f}")
        else:
            print(f"  {label:<38} {'--':>8} {'--':>10} {'--':>8} {'--':>14} {'--':>12}")

    print(f"\n  BSE SENSEX Combined Net PnL: INR {total_pnl:+,.0f}")
    print("="*80 + "\n")
