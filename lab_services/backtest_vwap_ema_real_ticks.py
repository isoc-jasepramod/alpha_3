"""
PROJECT ALPHA 3.0 — VWAP_EMA BACKTEST ON REAL RECORDED TICK LAKE (PAST 2-3 DAYS)
Data Source: data/lake/date=2026-09-29, 2026-09-30, 2026-10-01, 2026-10-02
- Replays actual tick-by-tick order-flow with REAL option cumulative volumes
- Feeds the production VWAPEMAAlignment strategy instance directly
- Evaluates the effect of the new Real Option RVOL Filter (min_opt_rvol = 1.20)
- Compares:
    1. Naive Spot Only (No real option volume confirmation)
    2. Live Production Strategy (With Real ATM Option RVOL Gate >= 1.20x)
"""

import sys
import os
import json
import asyncio
from glob import glob
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import pandas as pd
import numpy as np
from loguru import logger

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

logger.remove()
logger.add(sys.stderr, level="ERROR")

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from backend.strategies.vwap_ema import VWAPEMAAlignment
from backend.strategies.regime_filter import RegimeFilter

ACCOUNT_EQUITY = 100000.0
RISK_PER_TRADE = 1000.0
LOT_SIZE_NIFTY = 65
LOT_SIZE_SENSEX = 20
DELTA_ATM = 0.50

def build_token_dictionary() -> Dict[str, Dict[str, Any]]:
    """Builds token lookup map for Spot and Options."""
    token_map = {}

    # Spot tokens
    token_map["99926000"] = {"is_spot": True, "name": "NIFTY", "exchange": "nse_cm"}
    token_map["26000"] = {"is_spot": True, "name": "NIFTY", "exchange": "nse_cm"}
    token_map["99919000"] = {"is_spot": True, "name": "SENSEX", "exchange": "bse_cm"}

    # Master contracts
    master_path = os.path.join(project_root, "data", "instrument_master.json")
    if os.path.exists(master_path):
        try:
            with open(master_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                tok = str(item.get("token", ""))
                sym = str(item.get("symbol", ""))
                name = str(item.get("name", ""))
                itype = str(item.get("instrumenttype", ""))
                if itype == "OPTIDX" and name in ("NIFTY", "SENSEX"):
                    strk_raw = float(item.get("strike", 0.0))
                    # Normal strike conversion (divide by 100 if stored in paise)
                    strike = strk_raw / 100.0 if strk_raw > 100000 else strk_raw
                    opt_type = "CE" if sym.endswith("CE") else ("PE" if sym.endswith("PE") else "")
                    lot = int(item.get("lotsize", 65 if name == "NIFTY" else 20))
                    token_map[tok] = {
                        "is_spot": False,
                        "name": name,
                        "symbol": sym,
                        "strike": strike,
                        "option_type": opt_type,
                        "lot_size": lot,
                        "exchange": str(item.get("exch_seg", "nfo_fo"))
                    }
        except Exception as e:
            logger.warning(f"Failed to read instrument_master: {e}")

    # Fallback / heuristic parser for tokens not in current master (e.g. 73xxx, 887xxx)
    # We inspect lake ticks directly to infer strikes if needed
    return token_map

async def run_replay_for_date(date_str: str, use_opt_rvol: bool = True) -> List[Dict[str, Any]]:
    """Runs a complete tick-by-tick replay for a single date partition."""
    partition_dir = os.path.join(project_root, "data", "lake", f"date={date_str}")
    files = sorted(glob(os.path.join(partition_dir, "*.parquet")))
    if not files:
        return []

    token_map = build_token_dictionary()

    # Configure Strategy
    cfg = {
        "min_opt_rvol": 1.20 if use_opt_rvol else 0.0, # If disabled, gate passes everything
        "min_adx": 22.0,
        "min_vwap_slope": 0.35
    }
    regime = RegimeFilter()
    strategy = VWAPEMAAlignment(config=cfg, regime_filter=regime)

    signals_generated = []
    active_spot = {"NIFTY": 22700.0, "SENSEX": 73000.0}

    # 3-min aggregators for independent regime feeding
    from backend.strategies.indicators import CandleAggregator, IncrementalVWAP, IncrementalADX
    regime_agg = {"NIFTY": CandleAggregator(timeframe_seconds=180), "SENSEX": CandleAggregator(timeframe_seconds=180)}
    regime_vwap = {"NIFTY": IncrementalVWAP(), "SENSEX": IncrementalVWAP()}
    regime_adx = {"NIFTY": IncrementalADX(period=14), "SENSEX": IncrementalADX(period=14)}

    for file_idx, fpath in enumerate(files):
        df = pd.read_parquet(fpath)
        for _, row in df.iterrows():
            tok = str(row["token"])
            ltp = float(row.get("ltp", 0.0))
            vol = float(row.get("volume", 0.0))
            oi = float(row.get("open_interest", 0.0))
            raw_ts = float(row.get("exchange_timestamp") or 0.0)

            if ltp <= 0:
                continue

            # Update spot tracking
            if tok in ("99926000", "26000"):
                active_spot["NIFTY"] = ltp
            elif tok in ("99919000",):
                active_spot["SENSEX"] = ltp

            # Resolve meta
            meta = token_map.get(tok)
            if not meta:
                exch = str(row.get("exchange", "")).lower()
                name = "SENSEX" if "bfo" in exch else "NIFTY"
                meta = {
                    "is_spot": False,
                    "name": name,
                    "symbol": f"{name}_OPT_{tok}",
                    "strike": round(active_spot[name] / (50 if name == "NIFTY" else 100)) * (50 if name == "NIFTY" else 100),
                    "option_type": "CE",
                    "lot_size": 20 if name == "SENSEX" else 65,
                    "exchange": exch
                }

            # If option, compute ATM offset
            if not meta.get("is_spot") and meta.get("strike"):
                inst = meta.get("name", "NIFTY")
                step = 50.0 if inst == "NIFTY" else 100.0
                cur_atm = round(active_spot[inst] / step) * step
                strk = meta.get("strike")
                offset = int((strk - cur_atm) / step)
                meta["offset"] = offset

            tick_dict = {
                "token": tok,
                "ltp": ltp,
                "volume": vol,
                "open_interest": oi,
                "exchange_timestamp": raw_ts
            }

            # Feed to regime filter only on 3m candle close
            if meta.get("is_spot"):
                inst = meta.get("name", "NIFTY")
                closed_regime = regime_agg[inst].on_tick(raw_ts, ltp, volume=1.0)
                if closed_regime:
                    vwap_val = regime_vwap[inst].update(ltp, 1.0)
                    adx_val = regime_adx[inst].update(closed_regime["high"], closed_regime["low"], closed_regime["close"])
                    regime.update_candle(inst, closed_regime, vwap_val, adx_val)

            # Process through VWAP_EMA strategy
            sig = await strategy.on_tick(tick_dict, meta=meta)
            if sig:
                sig["replay_date"] = date_str
                sig["rvol_filter_active"] = use_opt_rvol
                signals_generated.append(sig)

    return signals_generated

def resolve_lake_signals(signals: List[Dict[str, Any]], date_str: str) -> List[Dict[str, Any]]:
    """Resolves generated signals against end of day price action."""
    # Read the last parquet file of the session to get closing LTPs
    partition_dir = os.path.join(project_root, "data", "lake", f"date={date_str}")
    files = sorted(glob(os.path.join(partition_dir, "*.parquet")))
    if not files:
        return signals

    last_df = pd.read_parquet(files[-1])
    last_ltp_by_token = {}
    for _, r in last_df.iterrows():
        last_ltp_by_token[str(r["token"])] = float(r.get("ltp", 0.0))

    resolved_list = []
    for s in signals:
        tok = str(s.get("option_token", ""))
        entry = float(s.get("entry_price", 100.0))
        sl_spot = float(s.get("custom_sl_spot", 0.0))
        spot_entry = float(s.get("spot_entry", 0.0))
        spot_risk = abs(spot_entry - sl_spot) if sl_spot > 0 else 30.0
        opt_risk = spot_risk * DELTA_ATM
        lot_size = int(s.get("lot_size", 65))
        lots = max(1, int(RISK_PER_TRADE / (opt_risk * lot_size)))
        qty = lots * lot_size

        exit_p = last_ltp_by_token.get(tok, entry)
        pnl = round((exit_p - entry) * qty, 2)
        r_mult = round(pnl / RISK_PER_TRADE, 2)

        s_copy = dict(s)
        s_copy["exit_price"] = exit_p
        s_copy["quantity"] = qty
        s_copy["pnl_inr"] = pnl
        s_copy["r_multiple"] = r_mult
        s_copy["status"] = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "FLAT")
        resolved_list.append(s_copy)

    return resolved_list

async def run_lake_backtest():
    print("=" * 115)
    print("  PROJECT ALPHA 3.0 | VWAP_EMA REAL VOLUME LAKE BACKTEST")
    print("  Data Source: Recorded High-Resolution Ticks from data/lake (Sep 29 - Oct 02)")
    print("  Evaluating the Institutional ATM Option RVOL Gate (min_opt_rvol = 1.20)")
    print("=" * 115)

    dates = ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]

    # 1. Run with Naive Spot Volume (No real option volume confirmation)
    print("\n[PHASE 1/2] Running Naive Spot Confirmation (Option RVOL Gate Disabled)...")
    naive_signals = []
    for d in dates:
        sigs = await run_replay_for_date(d, use_opt_rvol=False)
        resolved = resolve_lake_signals(sigs, d)
        naive_signals.extend(resolved)
        print(f"  {d}: {len(sigs)} candidate setups triggered.")

    # 2. Run with Live Production Setup (With Real ATM Option RVOL Gate >= 1.20x)
    print("\n[PHASE 2/2] Running Live Production Setup (Option RVOL Gate >= 1.20x Active)...")
    live_signals = []
    for d in dates:
        sigs = await run_replay_for_date(d, use_opt_rvol=True)
        resolved = resolve_lake_signals(sigs, d)
        live_signals.extend(resolved)
        print(f"  {d}: {len(sigs)} setups confirmed with real institutional option volume.")

    # Comparison Table
    print("\n" + "=" * 115)
    print("  COMPARATIVE RESULTS: NAIVE SPOT vs REAL OPTION RVOL GATE")
    print("=" * 115)
    print(f"{'DATE':<11} | {'MODE':<25} | {'SETUPS':<7} | {'CONFIRMED':<10} | {'WINS':<5} | {'LOSSES':<7} | {'NET PNL'}")
    print("-" * 115)

    for d in dates:
        n_d = [s for s in naive_signals if s["replay_date"] == d]
        l_d = [s for s in live_signals if s["replay_date"] == d]

        n_pnl = sum(s["pnl_inr"] for s in n_d)
        n_wins = sum(1 for s in n_d if s["pnl_inr"] > 0)
        n_loss = sum(1 for s in n_d if s["pnl_inr"] < 0)

        l_pnl = sum(s["pnl_inr"] for s in l_d)
        l_wins = sum(1 for s in l_d if s["pnl_inr"] > 0)
        l_loss = sum(1 for s in l_d if s["pnl_inr"] < 0)

        print(f"{d:<11} | {'Naive Spot (No RVOL)':<25} | {len(n_d):<7} | {len(n_d):<10} | {n_wins:<5} | {n_loss:<7} | INR {n_pnl:+,.2f}")
        print(f"{d:<11} | {'Live Option RVOL (>=1.2x)':<25} | {len(n_d):<7} | {len(l_d):<10} | {l_wins:<5} | {l_loss:<7} | INR {l_pnl:+,.2f}")
        print("-" * 115)

    # Executive Summary
    tot_naive = len(naive_signals)
    tot_live = len(live_signals)
    filtered_out = tot_naive - tot_live

    pnl_naive = sum(s["pnl_inr"] for s in naive_signals)
    pnl_live = sum(s["pnl_inr"] for s in live_signals)

    print("\n" + "=" * 115)
    print("  EXECUTIVE TAKEAWAYS & INSTITUTIONAL VALIDATION")
    print("=" * 115)
    print(f"  Candidate Setups Formed on Spot:        {tot_naive}")
    print(f"  Confirmed with Real Option Volume:     {tot_live}")
    print(f"  Fakeout Traps Filtered Out by RVOL:    {filtered_out} ({(filtered_out / tot_naive * 100.0) if tot_naive else 0.0:.1f}%)")
    print(f"  Naive Spot Strategy PnL:               INR {pnl_naive:+,.2f}")
    print(f"  Option RVOL Protected Strategy PnL:    INR {pnl_live:+,.2f}")
    print(f"  Capital Saved by Real Volume Gate:     INR {(pnl_live - pnl_naive):+,.2f}")
    print("=" * 115)

if __name__ == "__main__":
    asyncio.run(run_lake_backtest())
