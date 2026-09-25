from abc import ABC, abstractmethod
from datetime import datetime, time, timezone, timedelta
from typing import Dict, Any, Optional
from collections import deque
import uuid

IST = timezone(timedelta(hours=5, minutes=30))

CONFIDENCE_THRESHOLD = 60
ELEVATED_CONFIDENCE_THRESHOLD = 75

class BaseStrategy(ABC):
    def __init__(self, name: str, enabled: bool = True, confidence_threshold: int = CONFIDENCE_THRESHOLD):
        self.name = name
        self.enabled = enabled
        self.confidence_threshold = confidence_threshold
        self._base_confidence_threshold = confidence_threshold
        self.last_signal_time: Dict[str, float] = {} # key -> timestamp
        self.cooldown_sec = 60 # Cooldown to avoid duplicate signals on identical strike

        # Resolution feedback tracking
        self._recent_outcomes: Dict[str, deque] = {}  # "NIFTY_PE" -> deque of (ts, status)
        self._consecutive_stops: Dict[str, int] = {}   # "NIFTY_PE" -> count
        self._last_stop_ts: Dict[str, float] = {}      # "NIFTY_PE" -> timestamp
        self._stop_cooldown_sec = 900  # 15-minute cooldown after 2 consecutive stops
        self._max_consecutive_stops = 2

        # Pre-Move Radar Alerts
        self.pending_alerts: deque = deque(maxlen=20)
        self._alert_cooldowns: Dict[str, float] = {}  # key -> timestamp
        self._alert_cooldown_sec = 75  # 75s cooldown to prevent repeated alerts on same setup


    def parse_ist_time(self, ts: float) -> datetime:
        """Converts unix timestamp to Indian Standard Time (IST)"""
        return datetime.fromtimestamp(ts, tz=IST)

    def is_time_gated(self, dt: datetime, start_time_str: str, end_time_str: str) -> bool:
        """Checks if given datetime falls within start_time and end_time (HH:MM:SS) in IST"""
        # If dt has no tzinfo or is UTC, convert to IST
        if dt.tzinfo != IST:
            dt = dt.astimezone(IST)

        sh, sm, ss = map(int, start_time_str.split(":"))
        eh, em, es = map(int, end_time_str.split(":"))
        
        t = dt.time()
        start = time(sh, sm, ss)
        end = time(eh, em, es)
        return start <= t <= end

    def can_trigger(self, key: str, now_ts: float) -> bool:
        last = self.last_signal_time.get(key, 0)
        if now_ts - last >= self.cooldown_sec:
            self.last_signal_time[key] = now_ts
            return True
        return False

    def emit_radar_alert(
        self,
        alert_type: str,
        instrument: str,
        direction: str,
        title: str,
        message: str,
        meta_details: Optional[Dict[str, Any]] = None,
        now_ts: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Emits a pre-move heads-up alert onto the radar queue for real-time WebSocket broadcast.
        """
        ts = now_ts or datetime.now(timezone.utc).timestamp()
        key = f"{self.name}_{instrument}_{direction}_{alert_type}"
        last_emitted = self._alert_cooldowns.get(key, 0)
        if ts - last_emitted < self._alert_cooldown_sec:
            return None

        self._alert_cooldowns[key] = ts
        alert = {
            "id": f"RADAR-{int(ts)}-{instrument}-{direction}-{str(uuid.uuid4())[:4].upper()}",
            "strategy": self.name,
            "instrument": instrument,
            "direction": direction,
            "alert_type": alert_type,
            "title": title,
            "message": message,
            "timestamp": ts,
            "expires_in_sec": 75,
            "details": meta_details or {}
        }
        self.pending_alerts.append(alert)
        from loguru import logger
        logger.info(f"📡 [RADAR PRE-ALERT] {title}: {message}")
        return alert


    def is_in_stop_cooldown(self, instrument: str, direction: str, now_ts: float) -> bool:
        """Check if this instrument+direction combo is in a stop-loss cooldown period."""
        key = f"{instrument}_{direction}"
        consec = self._consecutive_stops.get(key, 0)
        if consec >= self._max_consecutive_stops:
            last_ts = self._last_stop_ts.get(key, 0)
            elapsed = now_ts - last_ts
            if elapsed < self._stop_cooldown_sec:
                from loguru import logger
                remaining = int(self._stop_cooldown_sec - elapsed)
                logger.info(
                    f"⏸️ [{self.name}] Cooldown active for {key}: "
                    f"{consec} consecutive stops. {remaining}s remaining."
                )
                return True
            else:
                # Cooldown expired, reset
                self._consecutive_stops[key] = 0
        return False

    def notify_resolution(self, signal: Dict[str, Any]):
        """
        Called by SignalTracker when a signal resolves.
        Updates consecutive stop tracking and dynamic confidence threshold.
        """
        from loguru import logger

        strategy = signal.get("strategy", "")
        if strategy != self.name:
            return

        inst = signal.get("instrument", "NIFTY")
        direction = signal.get("direction", "CE")
        status = signal.get("status", "")
        key = f"{inst}_{direction}"
        now_ts = datetime.now(timezone.utc).timestamp()

        # Track outcome
        if key not in self._recent_outcomes:
            self._recent_outcomes[key] = deque(maxlen=5)
        self._recent_outcomes[key].append((now_ts, status))

        # Update consecutive stop counter
        if status == "STOP_HIT":
            self._consecutive_stops[key] = self._consecutive_stops.get(key, 0) + 1
            self._last_stop_ts[key] = now_ts
            logger.warning(
                f"📊 [{self.name}] {key} consecutive stops: {self._consecutive_stops[key]}"
            )
        elif status == "TARGET_HIT":
            # Reset on a win
            self._consecutive_stops[key] = 0
            logger.info(f"✅ [{self.name}] {key} consecutive stops reset (target hit)")

        # Dynamic confidence threshold adjustment
        outcomes = list(self._recent_outcomes[key])
        if len(outcomes) >= 4:
            wins = sum(1 for _, s in outcomes[-4:] if s == "TARGET_HIT")
            win_rate = wins / 4.0
            if win_rate < 0.25:
                self.confidence_threshold = ELEVATED_CONFIDENCE_THRESHOLD
                logger.warning(
                    f"📈 [{self.name}] Confidence threshold elevated to {ELEVATED_CONFIDENCE_THRESHOLD} "
                    f"for {key} (win rate {win_rate*100:.0f}% over last 4 signals)"
                )
            else:
                self.confidence_threshold = self._base_confidence_threshold

    @staticmethod
    def compute_confidence(conditions: Dict[str, Any]) -> int:
        """
        Universal confidence scoring system (0 - 100):
        - trend_alignment: +20 if True
        - volume_confirmation: +25 if True
        - momentum_strength: +0 to +20 (scaled from 0.0 to 1.0)
        - time_quality: +15 if True
        - context_filter: +20 if True
        """
        score = 0
        if conditions.get("trend_alignment"):
            score += 20
        if conditions.get("volume_confirmation"):
            score += 25
        mom = float(conditions.get("momentum_strength", 0.0))
        score += min(20, max(0, int(mom * 20)))
        if conditions.get("time_quality"):
            score += 15
        if conditions.get("context_filter"):
            score += 20
        # Volume dynamics bonuses (Volume Contact / Volume Dry-Up Ignition)
        score += int(conditions.get("volume_contact_bonus", 0))
        score += int(conditions.get("volume_dryup_bonus", 0))
        return min(100, score)


    def build_signal_payload(
        self,
        instrument: str,
        direction: str,
        strike: float,
        option_type: str,
        option_token: str,
        option_symbol: str,
        spot_entry: float,
        entry_price: float,
        lot_size: int,
        confidence: int = 100,
        custom_sl_spot: Optional[float] = None,
        meta_details: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Builds a raw candidate signal payload before Risk Governor validation."""
        details = dict(meta_details or {})
        details["confidence"] = confidence
        if custom_sl_spot is not None:
            details["custom_sl_spot"] = custom_sl_spot

        sig_id = f"SIG-{datetime.now().strftime('%Y%m%d%H%M%S')}-{instrument}-{option_type}-{str(uuid.uuid4())[:4].upper()}"
        return {
            "signal_id": sig_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "strategy": self.name,
            "instrument": instrument,
            "direction": direction,
            "strike": strike,
            "option_type": option_type,
            "option_token": option_token,
            "option_symbol": option_symbol,
            "spot_entry": spot_entry,
            "entry_price": entry_price,
            "lot_size": lot_size,
            "confidence": confidence,
            "custom_sl_spot": custom_sl_spot,
            "details": details
        }

    @abstractmethod
    async def on_tick(self, tick: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """Processes tick and returns candidate signal if condition triggered."""
        pass

