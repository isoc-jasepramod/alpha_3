import asyncio
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from collections import deque
from loguru import logger
from backend.core.redis_bus import RedisBus
from backend.core.instrument_manager import InstrumentManager

class OptionChainPoller:
    """
    Asynchronous Full Option Chain Poller.
    Polls broader option chain metrics via REST or aggregates live Redis tick chain state.
    Calculates Chain-Wide PCR Velocity, Max Pain, and Multi-Strike Short Covering Cascades.
    """

    def __init__(
        self,
        redis_bus: RedisBus,
        instrument_mgr: InstrumentManager,
        poll_interval_sec: int = 60
    ):
        self.redis_bus = redis_bus
        self.instrument_mgr = instrument_mgr
        self.poll_interval = poll_interval_sec
        self.running = False
        self._task: Optional[asyncio.Task] = None

        # Rolling strike snapshot: inst -> token -> {"strike": S, "type": "CE"|"PE", "oi": OI, "ltp": LTP, "ts": TS}
        self.strike_snapshots: Dict[str, Dict[str, Dict[str, Any]]] = {
            "NIFTY": {},
            "SENSEX": {}
        }

        # Rolling PCR history: inst -> deque of (ts, pcr_val)
        self.pcr_history: Dict[str, deque] = {
            "NIFTY": deque(maxlen=30),
            "SENSEX": deque(maxlen=30)
        }

        self.last_alert_ts: Dict[str, float] = {}

    async def start(self):
        self.running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(f"OptionChainPoller started (interval: {self.poll_interval}s).")

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
        logger.info("OptionChainPoller stopped.")

    def record_tick(self, inst: str, token: str, strike: float, opt_type: str, oi: float, ltp: float, ts: float):
        if inst not in self.strike_snapshots:
            self.strike_snapshots[inst] = {}
        if oi > 0 and ltp > 0:
            self.strike_snapshots[inst][token] = {
                "strike": strike,
                "type": opt_type,
                "oi": oi,
                "ltp": ltp,
                "ts": ts
            }

    async def _poll_loop(self):
        while self.running:
            try:
                await asyncio.sleep(self.poll_interval)
                now_ts = datetime.now(timezone.utc).timestamp()
                for inst in ["NIFTY", "SENSEX"]:
                    await self._evaluate_chain_metrics(inst, now_ts)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"OptionChainPoller evaluation error: {e}")

    async def _evaluate_chain_metrics(self, inst: str, now_ts: float):
        tokens = self.strike_snapshots.get(inst, {})
        if len(tokens) < 4:
            return

        total_ce_oi = 0.0
        total_pe_oi = 0.0
        active_strikes: Dict[float, Dict[str, float]] = {}

        for t, data in tokens.items():
            if (now_ts - data["ts"]) < 300.0: # active in last 5 mins
                strike = data["strike"]
                opt_type = data["type"]
                oi = data["oi"]
                if strike not in active_strikes:
                    active_strikes[strike] = {"CE": 0.0, "PE": 0.0}
                active_strikes[strike][opt_type] = oi

                if opt_type == "CE":
                    total_ce_oi += oi
                elif opt_type == "PE":
                    total_pe_oi += oi

        if total_ce_oi <= 0:
            return

        # 1. Chain PCR calculation
        pcr = total_pe_oi / total_ce_oi
        pcr_hist = self.pcr_history[inst]
        pcr_hist.append((now_ts, pcr))

        # Check PCR Velocity over 5-10 mins
        if len(pcr_hist) >= 5 and (now_ts - pcr_hist[0][0]) >= 240.0:
            old_pcr = pcr_hist[0][1]
            pcr_delta = pcr - old_pcr
            # Rapid collapse in PCR: Call buying + Put unwinding (Bullish)
            if pcr_delta <= -0.25 and (now_ts - self.last_alert_ts.get(f"PCR_COLLAPSE_{inst}", 0.0)) > 600.0:
                self.last_alert_ts[f"PCR_COLLAPSE_{inst}"] = now_ts
                await self._publish_radar_alert(
                    inst=inst,
                    direction="CE",
                    alert_type="CHAIN_PCR_VELOCITY_CE",
                    title=f"🚀 {inst} Chain PCR Velocity Collapse ({old_pcr:.2f} ➔ {pcr:.2f})",
                    message=f"{inst} Put-Call Ratio collapsed by {abs(pcr_delta):.2f} in last 5m. Massive Call accumulation and Call short-covering across the chain.",
                    now_ts=now_ts
                )
            # Rapid surge in PCR: Heavy Put writing or Call unwinding
            elif pcr_delta >= 0.25 and (now_ts - self.last_alert_ts.get(f"PCR_SURGE_{inst}", 0.0)) > 600.0:
                self.last_alert_ts[f"PCR_SURGE_{inst}"] = now_ts
                await self._publish_radar_alert(
                    inst=inst,
                    direction="PE",
                    alert_type="CHAIN_PCR_VELOCITY_PE",
                    title=f"🔻 {inst} Chain PCR Velocity Surge ({old_pcr:.2f} ➔ {pcr:.2f})",
                    message=f"{inst} Put-Call Ratio expanded by +{pcr_delta:.2f} in last 5m. Aggressive institutional Put demand surging.",
                    now_ts=now_ts
                )

        # 2. Max Pain Calculation
        min_loss = float("inf")
        max_pain_strike = 0.0
        sorted_strikes = sorted(active_strikes.keys())

        for test_k in sorted_strikes:
            total_loss = 0.0
            for k, ois in active_strikes.items():
                ce_oi = ois.get("CE", 0.0)
                pe_oi = ois.get("PE", 0.0)
                if test_k > k:
                    total_loss += ce_oi * (test_k - k)
                elif test_k < k:
                    total_loss += pe_oi * (k - test_k)
            if total_loss < min_loss:
                min_loss = total_loss
                max_pain_strike = test_k

        logger.debug(f"[CHAIN POLLER] {inst}: PCR={pcr:.2f}, MaxPain={max_pain_strike:.0f}, StrikesTracked={len(active_strikes)}")

    async def _publish_radar_alert(self, inst: str, direction: str, alert_type: str, title: str, message: str, now_ts: float):
        alert_payload = {
            "event": "RADAR_PRE_ALERT",
            "alert": {
                "strategy": "CHAIN_POLLER",
                "instrument": inst,
                "direction": direction,
                "alert_type": alert_type,
                "title": title,
                "message": message,
                "timestamp": now_ts,
                "iso_time": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()
            }
        }
        await self.redis_bus.publish_alert(alert_payload)
        logger.info(f"📢 [CHAIN POLLER RADAR] Emitted: {title}")
