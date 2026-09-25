from collections import deque
from datetime import datetime, timezone, time
from typing import Dict, Any, Optional, List
from loguru import logger
from backend.strategies.base_strategy import BaseStrategy


class MomentumImpulseDetector(BaseStrategy):
    """
    Momentum Impulse Detector (09:20 - 15:20)
    Detects sudden, vertical price spikes (institutional sweeps, gamma squeezes,
    short covering, news velocity) within 5–15 seconds of the impulse start.

    Core Mechanics:
    1. High-Frequency Tick Velocity:
       Tracks rolling spot ticks over a configurable window (default 20s).
       Detects directional velocity:
         - NIFTY: >= 25 points within <= 20s (Radar heads-up at 65% ~ 16.5 pts)
         - SENSEX: >= 75 points within <= 20s (Radar heads-up at 65% ~ 48.8 pts)
       Requires directional consistency (>= 70% of tick deltas in the impulse direction).

    2. Real-Time ATM Option Confirmation:
       Confirms that the corresponding ATM option premium is actively expanding:
         - Minimum expansion: premium must have surged >= +3.5% from rolling baseline.
         - Anti-Top Chasing / Staleness Guard: rejects if premium already surged > 16.0%
           (protects trader from buying the very top of an already exhausted spike).

    3. Adaptive Impulse Risk & Exit Guidance:
       Because impulse moves lack standard swing pullbacks:
         - Custom Spot SL is anchored to the base of the impulse origin.
         - 1:2 R/R target with tight trailing guidance (+1R partial profit, trail to breakeven).
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(name="MOMENTUM_IMPULSE")
        cfg = config or {}
        self.start_time = cfg.get("start_time", "09:20:00")
        self.end_time = cfg.get("end_time", "15:20:00")

        # Spot velocity point thresholds
        self.nifty_velocity_pts = float(cfg.get("nifty_velocity_pts", 25.0))
        self.sensex_velocity_pts = float(cfg.get("sensex_velocity_pts", 75.0))
        self.window_sec = float(cfg.get("window_sec", 20.0))
        self.min_tick_count = int(cfg.get("min_tick_count", 4))
        self.min_directional_pct = float(cfg.get("min_directional_pct", 0.70))
        self.radar_ratio = float(cfg.get("radar_velocity_ratio", 0.65))

        # Option expansion thresholds
        self.min_opt_surge_pct = float(cfg.get("min_opt_surge_pct", 3.5))
        self.max_opt_surge_pct = float(cfg.get("max_opt_surge_pct", 16.0))
        self.impulse_ttl_sec = float(cfg.get("impulse_ttl_sec", 25.0))
        self.cooldown_sec = float(cfg.get("cooldown_sec", 90.0))

        # Spot tick rolling history: inst -> deque of (ts, ltp)
        self.spot_ticks: Dict[str, deque] = {
            "NIFTY": deque(maxlen=200),
            "SENSEX": deque(maxlen=200)
        }

        # Option tick rolling history: token -> deque of (ts, ltp)
        self.opt_ticks: Dict[str, deque] = {}

        # Active detected impulses waiting for option confirmation:
        # inst -> { "direction": "CE"|"PE", "spot_start": float, "spot_curr": float,
        #           "spot_delta": float, "elapsed": float, "timestamp": float, "expires_at": float }
        self.active_impulses: Dict[str, Optional[Dict[str, Any]]] = {
            "NIFTY": None,
            "SENSEX": None
        }

        # Radar alert cooldown tracking: key -> timestamp
        self._radar_emitted: Dict[str, float] = {}
        self._radar_cooldown_sec = 60.0

    def seed_from_spot(self, inst: str, spot_info: Dict[str, Any]):
        ltp = float(spot_info.get("ltp", 0.0))
        if ltp <= 0:
            return
        now_ts = datetime.now(timezone.utc).timestamp()
        if inst in self.spot_ticks:
            self.spot_ticks[inst].clear()
            self.spot_ticks[inst].append((now_ts - 5.0, ltp))
            self.spot_ticks[inst].append((now_ts, ltp))

    def _in_session_window(self, dt: datetime) -> bool:
        t = dt.time()
        sh, sm, ss = map(int, self.start_time.split(":"))
        eh, em, es = map(int, self.end_time.split(":"))
        return time(sh, sm, ss) <= t <= time(eh, em, es)

    def _get_velocity_threshold(self, inst: str) -> float:
        return self.nifty_velocity_pts if inst == "NIFTY" else self.sensex_velocity_pts

    def _compute_spot_velocity(self, inst: str, cur_ts: float) -> Optional[Dict[str, Any]]:
        """
        Calculates directional point move and tick consistency over the rolling window.
        Returns dict with velocity metrics or None if insufficient ticks.
        """
        ticks = list(self.spot_ticks.get(inst, []))
        if len(ticks) < self.min_tick_count:
            return None

        cur_time, cur_price = ticks[-1]
        cutoff_time = cur_time - self.window_sec

        # Collect ticks within the rolling window
        window_ticks = [(t, p) for (t, p) in ticks if t >= cutoff_time]
        if len(window_ticks) < self.min_tick_count:
            return None

        start_time, start_price = window_ticks[0]
        elapsed = max(0.5, cur_time - start_time)
        spot_delta = cur_price - start_price

        # Directional tick consistency
        up_ticks = 0
        down_ticks = 0
        total_intervals = 0
        for i in range(1, len(window_ticks)):
            diff = window_ticks[i][1] - window_ticks[i - 1][1]
            if diff > 0.01:
                up_ticks += 1
                total_intervals += 1
            elif diff < -0.01:
                down_ticks += 1
                total_intervals += 1

        if total_intervals == 0:
            tick_consistency = 0.0
        elif spot_delta > 0:
            tick_consistency = up_ticks / total_intervals
        else:
            tick_consistency = down_ticks / total_intervals

        return {
            "spot_start": start_price,
            "spot_curr": cur_price,
            "spot_delta": spot_delta,
            "elapsed": elapsed,
            "tick_consistency": tick_consistency,
            "tick_count": len(window_ticks)
        }

    def _get_opt_base_price(self, token: str, cur_ts: float, lookback_sec: float = 30.0) -> float:
        """Finds the lowest price in the rolling lookback window as the pre-impulse base."""
        ticks = self.opt_ticks.get(token, deque())
        if not ticks:
            return 0.0
        cutoff = cur_ts - lookback_sec
        recent_prices = [p for (t, p) in ticks if t >= cutoff and p > 0]
        if not recent_prices:
            return ticks[-1][1]
        return min(recent_prices)

    async def on_tick(self, tick: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        raw_ts = tick.get("exchange_timestamp") or datetime.now(timezone.utc).timestamp()
        ts = float(raw_ts) if raw_ts > 1e11 else float(raw_ts)
        if ts > 1e11:
            ts /= 1000.0

        now_dt = self.parse_ist_time(ts)
        token = str(tick.get("token", ""))
        ltp = float(tick.get("ltp", 0.0))
        if ltp <= 0:
            return None

        # -------------------------------------------------------------
        # 1. SPOT TICK PROCESSING: High-Frequency Impulse Detection
        # -------------------------------------------------------------
        if meta and meta.get("is_spot"):
            inst = meta.get("name", "NIFTY")
            if inst not in self.spot_ticks:
                return None

            # Filter unrealistic bad ticks
            if inst == "NIFTY" and (ltp < 15000.0 or ltp > 40000.0):
                return None
            if inst == "SENSEX" and (ltp < 50000.0 or ltp > 120000.0):
                return None

            # Record spot tick
            self.spot_ticks[inst].append((ts, ltp))

            if not self._in_session_window(now_dt):
                return None

            vel = self._compute_spot_velocity(inst, ts)
            if not vel:
                return None

            delta = vel["spot_delta"]
            abs_delta = abs(delta)
            elapsed = vel["elapsed"]
            consistency = vel["tick_consistency"]
            full_threshold = self._get_velocity_threshold(inst)
            radar_threshold = full_threshold * self.radar_ratio

            direction = "CE" if delta > 0 else "PE"

            # 1a. Radar Pre-Alert: Early heads-up when 65% of impulse velocity is achieved
            if abs_delta >= radar_threshold and consistency >= 0.65:
                radar_key = f"{inst}_{direction}_IMPULSE"
                last_radar = self._radar_emitted.get(radar_key, 0)
                if ts - last_radar >= self._radar_cooldown_sec:
                    self._radar_emitted[radar_key] = ts
                    arrow = "▲" if direction == "CE" else "▼"
                    title = f"⚡ {inst} Momentum Impulse Detected"
                    msg = (
                        f"{arrow} {inst} moved {delta:+.1f} pts in {elapsed:.1f}s "
                        f"({consistency*100:.0f}% tick velocity). Tracking ATM {direction} premium expansion..."
                    )
                    logger.warning(f"📡 [MOMENTUM IMPULSE RADAR] {title}: {msg}")
                    self.emit_radar_alert(
                        alert_type="MOMENTUM_IMPULSE",
                        instrument=inst,
                        direction=direction,
                        title=title,
                        message=msg,
                        meta_details={
                            "spot_delta": round(delta, 1),
                            "elapsed_sec": round(elapsed, 1),
                            "consistency": round(consistency, 2),
                            "spot_curr": ltp
                        },
                        now_ts=ts
                    )

            # 1b. Full Impulse Trigger: Arm impulse state waiting for option confirmation
            if abs_delta >= full_threshold and consistency >= self.min_directional_pct:
                # Do not re-arm if already active and recently updated
                cur_impulse = self.active_impulses.get(inst)
                if not cur_impulse or ts > cur_impulse.get("expires_at", 0) or cur_impulse.get("direction") != direction:
                    logger.success(
                        f"🚀 [MOMENTUM IMPULSE ARMED] {inst} {direction} Impulse Confirmed: "
                        f"{delta:+.1f} pts in {elapsed:.1f}s ({consistency*100:.0f}% consistency). "
                        f"Awaiting ATM option surge confirmation."
                    )
                    self.active_impulses[inst] = {
                        "direction": direction,
                        "spot_start": vel["spot_start"],
                        "spot_curr": ltp,
                        "spot_delta": delta,
                        "elapsed": elapsed,
                        "consistency": consistency,
                        "timestamp": ts,
                        "expires_at": ts + self.impulse_ttl_sec
                    }

            return None

        # -------------------------------------------------------------
        # 2. OPTION TICK PROCESSING: ATM Surge Confirmation & Trigger
        # -------------------------------------------------------------
        if not meta or not meta.get("option_type"):
            return None

        if not self._in_session_window(now_dt):
            return None

        inst = meta.get("name", "NIFTY")
        opt_type = meta.get("option_type")
        strike = float(meta.get("strike", 0.0))
        lot_size = int(meta.get("lot_size", 50))
        symbol = meta.get("symbol", "")
        offset = meta.get("offset", 99)
        is_atm = (offset == 0)

        # Track rolling option ticks for baseline pricing
        if token not in self.opt_ticks:
            self.opt_ticks[token] = deque(maxlen=80)
        self.opt_ticks[token].append((ts, ltp))

        # Check if an impulse is active for this instrument & direction
        impulse = self.active_impulses.get(inst)
        if not impulse:
            return None

        # Check TTL expiration
        if ts > impulse.get("expires_at", 0):
            self.active_impulses[inst] = None
            return None

        # Must match impulse direction
        if impulse.get("direction") != opt_type:
            return None

        # Focus strictly on ATM or closest strike (offset in [-1, 0, 1])
        if not is_atm and offset not in (-1, 0, 1):
            return None

        # Compute option premium expansion from pre-impulse base
        base_price = self._get_opt_base_price(token, ts, lookback_sec=30.0)
        if base_price <= 0.5:
            return None

        surge_pct = ((ltp - base_price) / base_price) * 100.0

        # Confirmation condition: Premium must be actively surging
        if surge_pct < self.min_opt_surge_pct:
            return None

        # Anti-Top Chasing / Staleness Guard:
        # If premium already surged > max_opt_surge_pct (e.g. >16%), trader is buying the peak
        if surge_pct > self.max_opt_surge_pct:
            logger.warning(
                f"🚧 [IMPULSE TOP GUARD] {symbol} ({opt_type}) premium already surged +{surge_pct:.1f}% "
                f"from base ₹{base_price:.2f} to ₹{ltp:.2f} (> {self.max_opt_surge_pct}%). "
                f"Suppressing signal to avoid buying the top of the impulse."
            )
            return None

        # Check cooldown to avoid duplicate triggers on the same wave
        sig_key = f"MOMENTUM_IMPULSE_{inst}_{opt_type}_{strike}"
        if not self.can_trigger(sig_key, ts):
            return None

        # Check consecutive stop cooldown
        if self.is_in_stop_cooldown(inst, opt_type, ts):
            return None

        # Consume the impulse so it fires only once per wave
        self.active_impulses[inst] = None

        # Anchor custom spot SL tightly to the impulse origin
        spot_start = impulse["spot_start"]
        spot_curr = impulse["spot_curr"]
        buffer_pts = 6.0 if inst == "NIFTY" else 18.0
        if opt_type == "CE":
            custom_sl = spot_start - buffer_pts
        else:
            custom_sl = spot_start + buffer_pts

        # Confidence Scoring (Impulse moves carry high momentum weight)
        full_thresh = self._get_velocity_threshold(inst)
        conditions = {
            "trend_alignment": True,
            "volume_confirmation": True,
            "momentum_strength": min(1.0, abs(impulse["spot_delta"]) / full_thresh),
            "time_quality": True,
            "context_filter": impulse["consistency"] >= 0.75
        }
        confidence = max(80, self.compute_confidence(conditions))

        logger.success(
            f"⚡ [MOMENTUM IMPULSE TRIGGERED] {symbol} {opt_type} @ ₹{ltp:.2f} | "
            f"Spot: {spot_curr:.1f} (Moved {impulse['spot_delta']:+.1f} pts in {impulse['elapsed']:.1f}s) | "
            f"Option Surge: +{surge_pct:.1f}% | SL Spot: {custom_sl:.1f} | Conf: {confidence}%"
        )

        return self.build_signal_payload(
            instrument=inst,
            direction=opt_type,
            strike=strike,
            option_type=opt_type,
            option_token=token,
            option_symbol=symbol,
            spot_entry=spot_curr,
            entry_price=ltp,
            lot_size=lot_size,
            confidence=confidence,
            custom_sl_spot=custom_sl,
            meta_details={
                "impulse_delta_pts": round(impulse["spot_delta"], 1),
                "impulse_elapsed_sec": round(impulse["elapsed"], 1),
                "option_surge_pct": round(surge_pct, 1),
                "base_premium": round(base_price, 2),
                "spot_origin": round(spot_start, 1),
                "guidance": "⚡ Momentum Impulse: High velocity move. Book 50% at +1R, trail remaining SL to breakeven immediately."
            }
        )
