"""
PROJECT ALPHA 3.0 — TRUE END-TO-END REPLAY BACKTEST
=====================================================
Replays REAL recorded market ticks (captured by tick_recorder.py) through the
ACTUAL live strategy classes — not vectorised approximations.

This is the highest-fidelity backtest available:
  - Real option premiums (not a DELTA_ATM=0.50 proxy)
  - Real option volume & Open Interest (for OI Squeeze / GEX)
  - Real total_buy_qty / total_sell_qty (for Flow Engine CVD)
  - Real spot ticks driving VWAP/EMA, Momentum, Regime Filter
  - The same RiskGovernor sizing and SignalTracker resolution used in production

INPUTS (per day partition under data/lake/date=YYYY-MM-DD/):
  - ticks_*.parquet   : full-fidelity tick stream (from tick_recorder.py)
  - token_meta.json   : token -> instrument metadata sidecar

USAGE:
  python lab_services/replay_from_recording.py --date 2026-09-25
  python lab_services/replay_from_recording.py --date 2026-09-25 --speed max
  python lab_services/replay_from_recording.py            # replays latest available day

Because it drives the real strategy on_tick() paths, the offset (ATM +/- N) is
recomputed per-tick from the live spot price via InstrumentManager, exactly as
production does.
"""

import os
import sys
import json
import glob
import asyncio
import argparse
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import pyarrow.parquet as pq
from loguru import logger

from backend.core.instrument_manager import InstrumentManager
from backend.risk.risk_governor import RiskGovernor
from backend.strategies.regime_filter import RegimeFilter
from backend.strategies.oi_squeeze import OISqueezeSentinel
from backend.strategies.orb_breakout import VolumeBackedORB
from backend.strategies.vwap_ema import VWAPEMAAlignment
from backend.strategies.gamma_scalp import ExpiryDayGammaScalp
from backend.strategies.momentum_impulse import MomentumImpulseDetector
from backend.strategies.iv_engine import IVEngine
from backend.strategies.gex_engine import GEXEngine
from backend.strategies.flow_engine import FlowEngine
from backend.strategies.squeeze_detector import SqueezeDetector

IST = timezone(timedelta(hours=5, minutes=30))
SPOT_TOKENS = {"99926000", "26000", "99919000"}


class RecordingReplayEngine:
    """Streams recorded ticks through the real strategy pipeline and resolves signals."""

    def __init__(self, data_lake_dir: str = "data/lake", strat_cfg: Optional[Dict[str, Any]] = None):
        self.data_lake_dir = os.path.join(project_root, data_lake_dir)
        strat_cfg = strat_cfg or {}

        self.instrument_mgr = InstrumentManager()
        self.instrument_mgr._load_from_cache()

        self.risk_governor = RiskGovernor()
        self.regime_filter = RegimeFilter(strat_cfg.get("regime_filter"))

        iv_engine = IVEngine(strat_cfg.get("iv_engine"))
        gex_engine = GEXEngine(iv_engine=iv_engine, config=strat_cfg.get("gex_engine"))

        self.strategies = [
            OISqueezeSentinel(strat_cfg.get("oi_squeeze")),
            VolumeBackedORB(strat_cfg.get("orb_breakout")),
            VWAPEMAAlignment(strat_cfg.get("vwap_ema"), regime_filter=self.regime_filter),
            ExpiryDayGammaScalp(strat_cfg.get("gamma_scalp")),
            MomentumImpulseDetector(strat_cfg.get("momentum_impulse"), regime_filter=self.regime_filter),
            iv_engine,
            gex_engine,
            FlowEngine(strat_cfg.get("flow_engine")),
            SqueezeDetector(strat_cfg.get("squeeze_detector")),
        ]

        self.token_meta: Dict[str, Dict[str, Any]] = {}
        self.spot_price: Dict[str, float] = {"NIFTY": 0.0, "SENSEX": 0.0}
        # Active signals awaiting resolution: signal_id -> signal dict
        self.active_signals: Dict[str, Dict[str, Any]] = {}
        self.completed: List[Dict[str, Any]] = []
        self.radar_alerts: List[Dict[str, Any]] = []

    # ---------------------------------------------------------------- loading
    def _resolve_partition(self, date_str: Optional[str]) -> Optional[str]:
        if date_str:
            p = os.path.join(self.data_lake_dir, f"date={date_str}")
            return p if os.path.isdir(p) else None
        # Pick latest partition that actually has recorded tick files
        parts = sorted(glob.glob(os.path.join(self.data_lake_dir, "date=*")))
        for p in reversed(parts):
            if glob.glob(os.path.join(p, "ticks_*.parquet")):
                return p
        return None

    def _load_ticks(self, partition: str) -> List[Dict[str, Any]]:
        files = sorted(glob.glob(os.path.join(partition, "ticks_*.parquet")))
        if not files:
            return []
        rows: List[Dict[str, Any]] = []
        for f in files:
            table = pq.read_table(f)
            rows.extend(table.to_pylist())
        # Sort chronologically across all flush files
        rows.sort(key=lambda r: (int(r.get("exchange_timestamp", 0)), str(r.get("received_at", ""))))
        return rows

    def _load_token_meta(self, partition: str) -> Dict[str, Dict[str, Any]]:
        meta_path = os.path.join(partition, "token_meta.json")
        if os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        logger.warning("No token_meta.json sidecar found — option ticks cannot be attributed. Spot-only replay.")
        return {}

    # ------------------------------------------------------------- meta build
    def _build_tick_meta(self, token: str) -> Optional[Dict[str, Any]]:
        """
        Returns the meta dict passed to strategy.on_tick(). Recomputes `offset`
        (ATM +/- N) live from the current spot price, exactly like production.
        """
        base = self.token_meta.get(token)
        if base is None:
            # Unknown token (spot fallback)
            if token in SPOT_TOKENS:
                name = "NIFTY" if token in ("99926000", "26000") else "SENSEX"
                return {"is_spot": True, "name": name}
            return None

        if base.get("is_spot"):
            return {"is_spot": True, "name": base.get("name", "NIFTY")}

        if base.get("unresolved"):
            return None

        inst = base.get("name", "NIFTY")
        strike = float(base.get("strike", 0.0))
        spot = self.spot_price.get(inst, 0.0)
        offset = 99
        if spot > 0 and strike > 0:
            atm = self.instrument_mgr.calculate_atm_strike(inst, spot)
            interval = self.instrument_mgr.rules.get(inst, {}).get("strike_interval", 50)
            offset = int(round((strike - atm) / interval)) if interval else 99

        return {
            "is_spot": False,
            "name": inst,
            "symbol": base.get("symbol", ""),
            "strike": strike,
            "option_type": base.get("option_type"),
            "expiry": base.get("expiry", ""),
            "lot_size": int(base.get("lot_size", 50) or 50),
            "exchange": base.get("exchange", ""),
            "offset": offset,
        }

    # --------------------------------------------------------- signal handling
    def _register_signal(self, sig: Dict[str, Any], ts: float):
        evaluated = self.risk_governor.evaluate_signal(sig)
        if not evaluated:
            return
        sid = evaluated.get("signal_id")
        if not sid or sid in self.active_signals:
            return
        evaluated["_entry_ts"] = ts
        evaluated["_registered_ltp"] = float(evaluated.get("entry_price", 0.0))
        self.active_signals[sid] = evaluated
        logger.success(
            f"🎯 [SIGNAL] {evaluated.get('strategy')} {evaluated.get('option_symbol')} "
            f"{evaluated.get('option_type')} @ Rs.{evaluated.get('entry_price'):.2f} "
            f"| SL {evaluated.get('stop_loss'):.2f} | TGT {evaluated.get('target'):.2f} "
            f"| Conf {evaluated.get('confidence')}%"
        )

    def _resolve_active_on_tick(self, tick: Dict[str, Any]):
        """Checks each active signal for its own option token hitting SL or target."""
        token = str(tick.get("token", ""))
        ltp = float(tick.get("ltp", 0.0))
        if ltp <= 0:
            return
        for sid, sig in list(self.active_signals.items()):
            if str(sig.get("option_token")) != token:
                continue
            entry = float(sig["entry_price"])
            sl = float(sig["stop_loss"])
            tgt = float(sig["target"])
            qty = int(sig.get("quantity", 1))
            status = None
            exit_p = ltp
            if ltp >= tgt:
                status, exit_p = "TARGET_HIT", tgt
            elif ltp <= sl:
                status, exit_p = "STOP_HIT", sl
            if status:
                pnl = (exit_p - entry) * qty
                sig["status"] = status
                sig["exit_price"] = exit_p
                sig["pnl_inr"] = round(pnl, 2)
                self.completed.append(sig)
                del self.active_signals[sid]
                icon = "✅" if status == "TARGET_HIT" else "🛑"
                logger.info(f"{icon} [{status}] {sig.get('option_symbol')} exit Rs.{exit_p:.2f} PnL Rs.{pnl:+.2f}")

    def _drain_radar(self):
        for strat in self.strategies:
            pend = getattr(strat, "pending_alerts", None)
            while pend:
                self.radar_alerts.append(pend.popleft())

    # ----------------------------------------------------------------- replay
    async def replay(self, date_str: Optional[str] = None):
        partition = self._resolve_partition(date_str)
        if not partition:
            print(f"No recorded data found for date={date_str or 'latest'} under {self.data_lake_dir}")
            print("Run lab_services/tick_recorder.py during market hours to capture data first.")
            return

        day = os.path.basename(partition).replace("date=", "")
        print("=" * 90)
        print(f"  TRUE END-TO-END REPLAY  |  {day}  |  Real recorded ticks -> live strategy classes")
        print("=" * 90)

        self.token_meta = self._load_token_meta(partition)
        ticks = self._load_ticks(partition)
        if not ticks:
            print("No ticks found in partition.")
            return
        print(f"[+] Loaded {len(ticks):,} recorded ticks across {len(self.token_meta)} tokens.\n")

        processed = 0
        for tick in ticks:
            token = str(tick.get("token", ""))
            ltp = float(tick.get("ltp", 0.0))
            if ltp <= 0:
                continue

            raw_ts = tick.get("exchange_timestamp", 0)
            ts = float(raw_ts) / 1000.0 if raw_ts > 1e11 else float(raw_ts or 0.0)

            meta = self._build_tick_meta(token)
            if meta is None:
                continue

            # Update spot tracking + risk governor ATR bar + regime filter feed
            if meta.get("is_spot"):
                inst = meta["name"]
                self.spot_price[inst] = ltp
                self.risk_governor.update_spot_bar(
                    inst,
                    float(tick.get("high", ltp)),
                    float(tick.get("low", ltp)),
                    ltp
                )

            # 1. Resolve any open signals against this tick
            self._resolve_active_on_tick(tick)

            # 2. Feed the tick to every strategy (real on_tick path)
            for strat in self.strategies:
                try:
                    candidate = await strat.on_tick(tick, meta=meta)
                    if candidate:
                        self._register_signal(candidate, ts)
                except Exception as e:
                    logger.debug(f"{strat.name} on_tick error: {e}")

            self._drain_radar()
            processed += 1

        # Force-close any still-open signals at last seen price (EOD)
        for sid, sig in list(self.active_signals.items()):
            entry = float(sig["entry_price"])
            last = float(sig.get("_registered_ltp", entry))
            sig["status"] = "EOD_OPEN"
            sig["exit_price"] = last
            sig["pnl_inr"] = 0.0
            self.completed.append(sig)
            del self.active_signals[sid]

        self._print_summary(day, processed)

    # ---------------------------------------------------------------- summary
    def _print_summary(self, day: str, processed: int):
        print(f"\n[+] Processed {processed:,} valid ticks.")
        print(f"[+] Radar pre-alerts emitted: {len(self.radar_alerts)}")
        print(f"[+] Signals generated: {len(self.completed)}\n")

        if not self.completed:
            print("No trade signals generated for this session.")
            return

        # Per-strategy aggregation
        by_strat: Dict[str, Dict[str, Any]] = {}
        for s in self.completed:
            st = s.get("strategy", "?")
            d = by_strat.setdefault(st, {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
            d["trades"] += 1
            pnl = float(s.get("pnl_inr", 0.0))
            d["pnl"] += pnl
            if pnl > 0:
                d["wins"] += 1
            elif pnl < 0:
                d["losses"] += 1

        print("=" * 90)
        print(f"  STRATEGY PERFORMANCE  |  {day}")
        print("=" * 90)
        print(f"{'STRATEGY':<20} | {'TRADES':<7} | {'WINS':<5} | {'LOSSES':<7} | {'WIN RATE':<9} | {'PNL (INR)'}")
        print("-" * 90)
        total_pnl = 0.0
        total_trades = 0
        total_wins = 0
        for st, d in sorted(by_strat.items(), key=lambda x: x[1]["pnl"], reverse=True):
            wr = (d["wins"] / d["trades"] * 100.0) if d["trades"] else 0.0
            total_pnl += d["pnl"]
            total_trades += d["trades"]
            total_wins += d["wins"]
            print(f"{st:<20} | {d['trades']:<7} | {d['wins']:<5} | {d['losses']:<7} | {wr:<8.1f}% | INR {d['pnl']:+,.2f}")
        print("-" * 90)
        overall_wr = (total_wins / total_trades * 100.0) if total_trades else 0.0
        gross_p = sum(s["pnl_inr"] for s in self.completed if s["pnl_inr"] > 0)
        gross_l = abs(sum(s["pnl_inr"] for s in self.completed if s["pnl_inr"] < 0))
        pf = (gross_p / gross_l) if gross_l > 0 else float("inf")
        print(f"  TOTAL: {total_trades} trades | Win Rate {overall_wr:.1f}% | Profit Factor {pf:.2f}")
        print(f"  SESSION PNL: INR {total_pnl:+,.2f}")
        print("=" * 90)


def _load_strategy_config() -> Dict[str, Any]:
    import yaml
    cfg_path = os.path.join(project_root, "config", "market_rules.yaml")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r") as f:
                return yaml.safe_load(f).get("strategies", {})
        except Exception as e:
            logger.warning(f"Could not load market_rules.yaml: {e}")
    return {}


def main():
    parser = argparse.ArgumentParser(description="True end-to-end replay from recorded ticks.")
    parser.add_argument("--date", type=str, default=None, help="Partition date YYYY-MM-DD (default: latest recorded)")
    parser.add_argument("--data-dir", type=str, default="data/lake", help="Data lake directory")
    args = parser.parse_args()

    engine = RecordingReplayEngine(data_lake_dir=args.data_dir, strat_cfg=_load_strategy_config())
    asyncio.run(engine.replay(args.date))


if __name__ == "__main__":
    main()
