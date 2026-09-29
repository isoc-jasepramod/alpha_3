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

    def __init__(self, config: Optional[Dict[str, Any]] = None, regime_filter: Optional[Any] = None):
        super().__init__(name="MOMENTUM_IMPULSE")
        cfg = config or {}
        self.regime_filter = regime_filter
        self.start_time = cfg.get("start_time", "09:20:00")
        self.end_time = cfg.get("end_time", "15:20:00")

        # Spot velocity point thresholds
        self.nifty_velocity_pts = float(cfg.get("nifty_velocity_pts", 25.0))
        self.sensex_velocity_pts = float(cfg.get("sensex_velocity_pts", 75.0))
        self.window_sec = float(cfg.get("window_sec", 20.0))
        self.min_tick_count = int(cfg.get("min_tick_count", 4))
        self.min_directional_pct = float(cfg.get("min_directional_pct", 0.70))
        # Lowered from 0.65 -> 0.50 so the heads-up fires earlier (at ~50% of full
        # velocity) giving the trader real lead time before the full impulse completes.
        self.radar_ratio = float(cfg.get("radar_velocity_ratio", 0.50))

        # --- Acceleration pre-alert (earliest heads-up) ---
        # Fires when velocity is *accelerating* (2nd derivative), which often precedes the
        # full impulse by a few seconds. This is the earliest, lowest-confidence heads-up.
        self.accel_enabled = bool(cfg.get("accel_enabled", True))
        # Min points-per-second in the recent sub-window to consider it accelerating.
        self.nifty_accel_min_recent_vel = float(cfg.get("nifty_accel_min_recent_vel", 2.5))
        self.sensex_accel_min_recent_vel = float(cfg.get("sensex_accel_min_recent_vel", 7.5))
        # Recent sub-window (s) whose velocity is compared against the older sub-window.
        self.accel_recent_window_sec = float(cfg.get("accel_recent_window_sec", 5.0))
        # Recent velocity must exceed older velocity by this ratio to count as accelerating.
        self.accel_ratio = float(cfg.get("accel_ratio", 1.6))

        # Option expansion thresholds
        self.min_opt_surge_pct = float(cfg.get("min_opt_surge_pct", 3.5))
        self.max_opt_surge_pct = float(cfg.get("max_opt_surge_pct", 16.0))
        self.impulse_ttl_sec = float(cfg.get("impulse_ttl_sec", 25.0))
        self.cooldown_sec = float(cfg.get("cooldown_sec", 90.0))

        # --- Post-impulse pullback entry (human-takeable) ---
        # Chasing the vertical spike is a race a human loses (radar->trigger was ~1s on
        # Sep 29). Instead, after an impulse arms we wait for the first shallow pullback
        # that HOLDS above the impulse origin, then signal — a slower, real entry with a
        # tight stop. The trader trades the pullback, not the spike.
        self.pullback_enabled = bool(cfg.get("pullback_enabled", True))
        # How far spot may retrace from the impulse peak, as a fraction of the impulse size.
        # e.g. 0.50 => a pullback of up to half the move still counts as a valid shallow dip.
        self.pullback_max_retrace = float(cfg.get("pullback_max_retrace", 0.50))
        # Pullback must "hold": spot must resume in the impulse direction by at least this
        # fraction of the impulse size off the pullback low/high before we signal.
        self.pullback_resume_frac = float(cfg.get("pullback_resume_frac", 0.15))
        # How long (s) after arming we keep waiting for a qualifying pullback.
        self.pullback_window_sec = float(cfg.get("pullback_window_sec", 90.0))

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

    def reset_session(self):
        """
        Clears all session-scoped state so nothing from day N leaks into day N+1
        when the app runs continuously across sessions without a restart.

        Resets:
          - Active impulses (including any pending pullback entries)
          - Spot and option tick buffers
          - Radar alert cooldowns
        """
        for inst in list(self.active_impulses.keys()):
            self.active_impulses[inst] = None
        for inst in list(self.spot_ticks.keys()):
            self.spot_ticks[inst].clear()
        self.opt_ticks.clear()
        self._radar_emitted.clear()
        logger.info("🔄 [MOMENTUM_IMPULSE] Session state reset for next trading day.")

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

    def _compute_acceleration(self, inst: str, cur_ts: float) -> Optional[Dict[str, Any]]:
        """
        Detects that spot is ACCELERATING: the velocity of the most recent sub-window is
        meaningfully higher than the velocity of the sub-window just before it, and both
        point the same way. This 2nd-derivative signal tends to precede the full impulse.

        Returns {"direction", "recent_vel", "older_vel"} or None.
        """
        ticks = list(self.spot_ticks.get(inst, []))
        if len(ticks) < self.min_tick_count + 1:
            return None

        cur_time = ticks[-1][0]
        w = self.accel_recent_window_sec
        recent_cut = cur_time - w
        older_cut = cur_time - (2.0 * w)

        recent = [(t, p) for (t, p) in ticks if t >= recent_cut]
        older = [(t, p) for (t, p) in ticks if older_cut <= t < recent_cut]
        if len(recent) < 2 or len(older) < 2:
            return None

        def _vel(seg):
            dt = max(0.5, seg[-1][0] - seg[0][0])
            return (seg[-1][1] - seg[0][1]) / dt  # pts/sec, signed

        recent_vel = _vel(recent)
        older_vel = _vel(older)

        # Must be moving in a consistent direction across both sub-windows.
        if recent_vel == 0.0 or (recent_vel > 0) != (older_vel >= 0):
            return None

        min_recent = self.nifty_accel_min_recent_vel if inst == "NIFTY" else self.sensex_accel_min_recent_vel
        if abs(recent_vel) < min_recent:
            return None

        # Accelerating: recent speed exceeds older speed by the required ratio.
        if abs(recent_vel) < abs(older_vel) * self.accel_ratio:
            return None

        return {
            "direction": "CE" if recent_vel > 0 else "PE",
            "recent_vel": recent_vel,
            "older_vel": older_vel
        }

    def _track_pullback(self, inst: str, ltp: float, ts: float):
        """
        Tracks the post-impulse pullback for an armed impulse in AWAITING_PULLBACK phase.
        Advances it to PULLBACK_READY (and emits an ACTIONABLE alert) once spot retraces
        shallowly from the impulse peak and then resumes in the impulse direction.
        """
        if not self.pullback_enabled:
            return
        imp = self.active_impulses.get(inst)
        if not imp or imp.get("phase") != "AWAITING_PULLBACK":
            return
        if ts > imp.get("expires_at", 0):
            return

        direction = imp["direction"]
        spot_start = imp["spot_start"]
        peak = imp.get("peak_spot", ltp)
        impulse_size = abs(peak - spot_start)
        if impulse_size <= 0.5:
            return

        max_retrace_pts = self.pullback_max_retrace * impulse_size
        resume_pts = self.pullback_resume_frac * impulse_size

        if direction == "CE":
            # Update peak if still rising (no pullback yet).
            if ltp >= peak:
                imp["peak_spot"] = ltp
                imp["pullback_extreme"] = ltp
                imp["spot_curr"] = ltp
                return
            # We are below the peak: track the deepest pullback low.
            imp["pullback_extreme"] = min(imp.get("pullback_extreme", ltp), ltp)
            retrace = peak - imp["pullback_extreme"]
            # Retrace must be a shallow dip, not a full reversal.
            if retrace <= 0 or retrace > max_retrace_pts:
                if retrace > max_retrace_pts:
                    # Reversal too deep — this impulse is dead; drop it.
                    self.active_impulses[inst] = None
                return
            # Resume: has price recovered up from the pullback low by resume_pts?
            resumed = ltp - imp["pullback_extreme"]
            if resumed >= resume_pts:
                self._promote_to_pullback_ready(inst, imp, ltp, ts, retrace)
        else:  # PE
            if ltp <= peak:
                imp["peak_spot"] = ltp
                imp["pullback_extreme"] = ltp
                imp["spot_curr"] = ltp
                return
            imp["pullback_extreme"] = max(imp.get("pullback_extreme", ltp), ltp)
            retrace = imp["pullback_extreme"] - peak
            if retrace <= 0 or retrace > max_retrace_pts:
                if retrace > max_retrace_pts:
                    self.active_impulses[inst] = None
                return
            resumed = imp["pullback_extreme"] - ltp
            if resumed >= resume_pts:
                self._promote_to_pullback_ready(inst, imp, ltp, ts, retrace)

    def _promote_to_pullback_ready(self, inst: str, imp: Dict[str, Any], ltp: float, ts: float, retrace: float):
        """Flip an armed impulse to PULLBACK_READY and emit a one-time ACTIONABLE alert."""
        imp["phase"] = "PULLBACK_READY"
        imp["pullback_entry_spot"] = ltp
        # Give the option confirmation a fresh, generous window from the pullback point.
        imp["expires_at"] = ts + self.impulse_ttl_sec
        imp["spot_curr"] = ltp
        direction = imp["direction"]
        if not imp.get("pullback_alerted"):
            imp["pullback_alerted"] = True
            arrow = "▲" if direction == "CE" else "▼"
            side = "higher-low" if direction == "CE" else "lower-high"
            title = f"🎯 {inst} Momentum Pullback Entry Ready ({direction})"
            msg = (
                f"{arrow} {inst} pulled back ~{retrace:.1f} pts after the impulse and is holding a "
                f"{side} at {ltp:.1f}. Human-takeable {direction} entry forming — awaiting ATM premium confirmation."
            )
            logger.success(f"🎯 [MOMENTUM PULLBACK READY] {title}: {msg}")
            self.emit_radar_alert(
                alert_type="MOMENTUM_PULLBACK_READY",
                instrument=inst,
                direction=direction,
                title=title,
                message=msg,
                meta_details={
                    "pullback_retrace_pts": round(retrace, 1),
                    "entry_spot": round(ltp, 1),
                    "impulse_origin": round(imp["spot_start"], 1),
                    "peak_spot": round(imp.get("peak_spot", ltp), 1)
                },
                now_ts=ts
            )

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

            # 1a-pre. Acceleration Pre-Alert: EARLIEST heads-up. Fires when spot velocity is
            # rising (2nd derivative), which often leads the full impulse by a few seconds.
            if self.accel_enabled:
                accel = self._compute_acceleration(inst, ts)
                if accel:
                    accel_dir = accel["direction"]
                    accel_key = f"{inst}_{accel_dir}_ACCEL"
                    last_accel = self._radar_emitted.get(accel_key, 0)
                    if ts - last_accel >= self._radar_cooldown_sec:
                        self._radar_emitted[accel_key] = ts
                        arrow = "▲" if accel_dir == "CE" else "▼"
                        title = f"⚡ {inst} Momentum Building (Accelerating)"
                        msg = (
                            f"{arrow} {inst} price velocity accelerating "
                            f"({accel['recent_vel']:+.1f} pts/s, up from {accel['older_vel']:+.1f}). "
                            f"An impulse may be starting — watch ATM {accel_dir}. NOT A TRADE yet."
                        )
                        self.emit_radar_alert(
                            alert_type="MOMENTUM_ACCELERATION",
                            instrument=inst,
                            direction=accel_dir,
                            title=title,
                            message=msg,
                            meta_details={
                                "recent_vel": round(accel["recent_vel"], 2),
                                "older_vel": round(accel["older_vel"], 2),
                                "spot_curr": ltp
                            },
                            now_ts=ts
                        )

            # 1a. Radar Pre-Alert: Early heads-up when ~50% of impulse velocity is achieved
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
            # Check Session Regime Filter: In CHOPPY regime, arming is suppressed (prevents entering into reversals)
            if abs_delta >= full_threshold and consistency >= self.min_directional_pct:
                if self.regime_filter:
                    allowed, reason = self.regime_filter.allows_momentum_arming(inst)
                    if not allowed:
                        logger.warning(
                            f"🛡️ [MOMENTUM CHOPPY SUPPRESSION] {inst} {direction} impulse ({delta:+.1f} pts in {elapsed:.1f}s) "
                            f"detected but arming SUPPRESSED: {reason}"
                        )
                        return None

                # Do not re-arm if already active and recently updated
                cur_impulse = self.active_impulses.get(inst)
                if not cur_impulse or ts > cur_impulse.get("expires_at", 0) or cur_impulse.get("direction") != direction:
                    # In pullback mode the impulse lives longer (we wait for a dip to hold),
                    # so use the pullback window as the TTL; otherwise the classic short TTL.
                    ttl = self.pullback_window_sec if self.pullback_enabled else self.impulse_ttl_sec
                    phase = "AWAITING_PULLBACK" if self.pullback_enabled else "AWAITING_SURGE"
                    logger.success(
                        f"🚀 [MOMENTUM IMPULSE ARMED] {inst} {direction} Impulse Confirmed: "
                        f"{delta:+.1f} pts in {elapsed:.1f}s ({consistency*100:.0f}% consistency). "
                        f"Phase: {phase}."
                    )
                    self.active_impulses[inst] = {
                        "direction": direction,
                        "spot_start": vel["spot_start"],
                        "spot_curr": ltp,
                        "spot_delta": delta,
                        "elapsed": elapsed,
                        "consistency": consistency,
                        "timestamp": ts,
                        "expires_at": ts + ttl,
                        "phase": phase,
                        "peak_spot": ltp,           # extreme reached in the impulse direction
                        "pullback_extreme": ltp,    # extreme of the retrace (low for CE, high for PE)
                        "pullback_alerted": False
                    }
                elif cur_impulse and cur_impulse.get("direction") == direction:
                    # Same-direction continuation: extend the peak so pullback is measured
                    # from the true top/bottom of the move.
                    if direction == "CE":
                        cur_impulse["peak_spot"] = max(cur_impulse.get("peak_spot", ltp), ltp)
                    else:
                        cur_impulse["peak_spot"] = min(cur_impulse.get("peak_spot", ltp), ltp)
                    cur_impulse["spot_curr"] = ltp

            # 1c. Pullback tracking: once armed and in AWAITING_PULLBACK, watch for a shallow
            # retrace that then resumes in the impulse direction. When it holds, flip the
            # phase to PULLBACK_READY and emit an ACTIONABLE alert the trader can actually act on.
            self._track_pullback(inst, ltp, ts)

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

        # Phase gate: in pullback mode, only fire once the pullback has held (PULLBACK_READY).
        # While still AWAITING_PULLBACK we deliberately do NOT signal — we are waiting for a
        # human-takeable dip rather than chasing the vertical spike.
        phase = impulse.get("phase", "AWAITING_SURGE")
        if phase == "AWAITING_PULLBACK":
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

        # Anchor custom spot SL. For a pullback entry, anchor to the pullback extreme (the
        # dip low for CE / spike high for PE) — a tight, well-defined stop that invalidates
        # the setup if the pullback fails. For a classic entry, anchor to the impulse origin.
        spot_start = impulse["spot_start"]
        spot_curr = impulse["spot_curr"]
        buffer_pts = 6.0 if inst == "NIFTY" else 18.0
        is_pullback_entry = impulse.get("phase") == "PULLBACK_READY"
        sl_anchor = impulse.get("pullback_extreme", spot_start) if is_pullback_entry else spot_start
        if opt_type == "CE":
            custom_sl = sl_anchor - buffer_pts
        else:
            custom_sl = sl_anchor + buffer_pts

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

        regime_st = self.regime_filter.get_regime(inst) if self.regime_filter else {}
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
                "entry_type": "PULLBACK" if is_pullback_entry else "IMPULSE",
                "regime": regime_st.get("regime", "UNKNOWN"),
                "regime_score": regime_st.get("score", 0.0),
                "guidance": (
                    "🎯 Pullback Entry: Entered on the first dip that held after the impulse. "
                    "Tight stop below the pullback low. Book 50% at +1R, trail to breakeven."
                    if is_pullback_entry else
                    "⚡ Momentum Impulse: High velocity move. Book 50% at +1R, trail remaining SL to breakeven immediately."
                )
            }
        )
