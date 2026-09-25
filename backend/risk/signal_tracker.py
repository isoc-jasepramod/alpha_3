import asyncio
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from loguru import logger
from sqlalchemy import select, update
from backend.database.db import AsyncSessionLocal
from backend.database.models import Signal, DailyJournal
from backend.risk.risk_governor import RiskGovernor
from backend.core.redis_bus import RedisBus
import yaml
import os

# Strategy-aware runaway thresholds
DEFAULT_RUNAWAY_PROFILE = {"surge_pct": 0.035, "time_window_sec": 30}

def _load_runaway_profiles() -> Dict[str, Dict[str, float]]:
    """Load per-strategy runaway profiles from settings.yaml."""
    config_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "settings.yaml")
    profiles = {"default": DEFAULT_RUNAWAY_PROFILE.copy()}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                cfg = yaml.safe_load(f).get("risk", {}).get("runaway_profiles", {})
                for key, val in cfg.items():
                    profiles[key] = {
                        "surge_pct": float(val.get("surge_pct", 0.035)),
                        "time_window_sec": int(val.get("time_window_sec", 30))
                    }
        except Exception as e:
            logger.warning(f"Error reading runaway profiles: {e}")
    return profiles


class SignalTracker:
    """
    Background worker that monitors active signals against incoming ticks.
    Enforces:
    1. Adaptive Runaway Guardrail: Strategy-aware surge thresholds (e.g. +8% for GAMMA_SCALP, +5% for VWAP_EMA)
    2. Target Hit: If LTP >= Target -> NO TRADE: TARGET HIT
    3. Stop Hit: If LTP <= Stop Loss -> NO TRADE: STOP HIT
    Updates PostgreSQL records and broadcasts state transitions to Redis.
    """

    def __init__(self, redis_bus: RedisBus, risk_governor: RiskGovernor):
        self.redis_bus = redis_bus
        self.risk_governor = risk_governor
        self.active_signals: Dict[str, Dict[str, Any]] = {} # signal_id -> signal_dict
        self.runaway_profiles = _load_runaway_profiles()

        # Resolution callback registry — strategies can register to receive outcome notifications
        self._resolution_callbacks = []

    def register_resolution_callback(self, callback):
        """Register a callback that gets called with (signal_dict) when a signal resolves."""
        self._resolution_callbacks.append(callback)

    def _get_runaway_params(self, sig: Dict[str, Any]) -> tuple:
        """Return (surge_pct, window_sec) based on the signal's strategy."""
        strategy = sig.get("strategy", "default")
        profile = self.runaway_profiles.get(strategy, self.runaway_profiles.get("default", DEFAULT_RUNAWAY_PROFILE))
        return profile["surge_pct"], profile["time_window_sec"]

    def register_signal(self, signal: Dict[str, Any]):
        sig_id = signal["signal_id"]
        # Save start timestamp
        sig = dict(signal)
        sig["registered_ts"] = datetime.now(timezone.utc).timestamp()
        sig["live_ltp"] = signal["entry_price"]
        sig["deviation_pct"] = 0.0
        self.active_signals[sig_id] = sig

        surge_pct, window_sec = self._get_runaway_params(sig)
        logger.info(
            f"Signal registered in tracker: {sig_id} ({sig['option_symbol']}) "
            f"[Runaway: +{surge_pct*100:.1f}% / {window_sec}s for {sig.get('strategy', '?')}]"
        )

    async def on_tick(self, tick: Dict[str, Any]):
        token = str(tick.get("token", ""))
        ltp = float(tick.get("ltp", 0.0))
        if ltp <= 0 or not self.active_signals:
            return

        now_ts = datetime.now(timezone.utc).timestamp()
        resolved_signals = []

        # Evict resolved signals older than 30 minutes (1800s)
        for sid, s in list(self.active_signals.items()):
            res_ts = s.get("resolved_ts")
            if res_ts and (now_ts - res_ts) > 1800:
                del self.active_signals[sid]

        for sig_id, sig in list(self.active_signals.items()):
            # If already resolved (Stop Hit / Target Hit / Chase Prevented), keep visible on UI and skip re-evaluation
            if sig.get("status") in ("STOP_HIT", "TARGET_HIT", "INVALID_CHASE_PREVENTED"):
                continue

            if str(sig.get("option_token")) != token:
                continue

            entry = float(sig["entry_price"])
            sl = float(sig["stop_loss"])
            tgt = float(sig["target"])
            reg_ts = float(sig.get("registered_ts", now_ts))
            elapsed_sec = now_ts - reg_ts
            qty = int(sig.get("quantity", 1))

            sig["live_ltp"] = ltp
            deviation_pct = ((ltp - entry) / entry) * 100.0
            sig["deviation_pct"] = round(deviation_pct, 2)
            sig["elapsed_sec"] = int(elapsed_sec)

            # 1. Adaptive Runaway Guardrail (strategy-aware thresholds)
            surge_pct, window_sec = self._get_runaway_params(sig)
            if elapsed_sec <= window_sec and (ltp >= entry * (1.0 + surge_pct)):
                logger.warning(
                    f"🚫 [CHASE PREVENTED] Signal {sig_id} ({sig.get('strategy', '?')}) "
                    f"surged +{deviation_pct:.1f}% within {elapsed_sec:.1f}s "
                    f"(threshold: +{surge_pct*100:.1f}% / {window_sec}s). Locking card."
                )
                sig["status"] = "INVALID_CHASE_PREVENTED"
                sig["exit_price"] = ltp
                sig["theoretical_pnl"] = 0.0
                sig["resolved_ts"] = now_ts
                resolved_signals.append((sig_id, sig, 0.0))
                continue

            # 2. Target Hit
            if ltp >= tgt:
                logger.success(f"🎯 [TARGET HIT] Signal {sig_id} hit Target ₹{tgt} at LTP ₹{ltp}!")
                pnl = (tgt - entry) * qty
                sig["status"] = "TARGET_HIT"
                sig["exit_price"] = tgt
                sig["theoretical_pnl"] = round(pnl, 2)
                sig["resolved_ts"] = now_ts
                resolved_signals.append((sig_id, sig, pnl))
                continue

            # 3. Stop Hit
            if ltp <= sl:
                logger.error(f"🛑 [STOP HIT] Signal {sig_id} hit SL ₹{sl} at LTP ₹{ltp}!")
                pnl = (sl - entry) * qty # negative
                sig["status"] = "STOP_HIT"
                sig["exit_price"] = sl
                sig["theoretical_pnl"] = round(pnl, 2)
                sig["resolved_ts"] = now_ts
                resolved_signals.append((sig_id, sig, pnl))
                continue

        # Process resolutions - keep in active_signals so card stays displayed on screen!
        for sig_id, sig, pnl in resolved_signals:
            self.active_signals[sig_id] = sig

            # Update Risk Governor realized PnL
            if pnl != 0.0:
                self.risk_governor.record_trade_result(pnl)

            # Persist to PostgreSQL
            await self._persist_signal_resolution(sig)

            # Broadcast state transition event to Redis
            await self.redis_bus.publish_signal({
                "event": "SIGNAL_RESOLVED",
                "signal": sig
            })

            # Notify registered strategy callbacks (resolution feedback loop)
            for cb in self._resolution_callbacks:
                try:
                    cb(sig)
                except Exception as e:
                    logger.error(f"Resolution callback error: {e}")

    async def _persist_signal_resolution(self, sig: Dict[str, Any]):
        try:
            async with AsyncSessionLocal() as session:
                stmt = select(Signal).where(Signal.signal_id == sig["signal_id"])
                res = await session.execute(stmt)
                db_sig = res.scalar_one_or_none()
                if db_sig:
                    db_sig.status = sig["status"]
                    db_sig.exit_price = sig.get("exit_price")
                    db_sig.theoretical_pnl = sig.get("theoretical_pnl")
                    db_sig.resolved_at = datetime.now(timezone.utc)
                    curr_details = dict(db_sig.details or {})
                    curr_details["exit_reason"] = sig["status"]
                    curr_details["exit_price"] = sig.get("exit_price")
                    curr_details["exit_pnl"] = sig.get("theoretical_pnl")
                    db_sig.details = curr_details
                    await session.commit()
                    logger.info(f"Updated DB signal {sig['signal_id']} status: {sig['status']} (Exit: {sig.get('exit_price')}, PnL: ₹{sig.get('theoretical_pnl')})")
        except Exception as e:
            logger.error(f"Failed to update DB for signal {sig.get('signal_id')}: {e}")
