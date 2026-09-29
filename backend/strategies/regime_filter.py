"""
PROJECT ALPHA 3.0 — REGIME FILTER ENGINE
Session-Level Institutional Regime Filter

Provides a shared session regime state for VWAP_EMA and MomentumImpulseDetector
using a composite 4-component score (0 to 100):

1. ADX Strength (30% weight):
   The trend power gauge, with a bonus for rising ADX.
   
2. Efficiency Ratio (30% weight):
   The cleanest chop vs. trend discriminator:
   ER = |Price_t - Price_{t-N}| / sum(|Price_i - Price_{i-1}|)
   ER -> 1.0 (straight line trend) vs ER -> 0.0 (erratic wandering chop).
   
3. VWAP Persistence (25% weight):
   Consistency of candle closes on one side of VWAP across the last 10 bars.
   Choppy days cross 5-10 times; trend days barely touch it.
   
4. VWAP Slope (15% weight):
   Rate of directional drift, scaled for NIFTY vs SENSEX.

The Gating Behavior:
- TRENDING_BULL / TRENDING_BEAR (Score >= 65): All setups allowed in VWAP_EMA and Momentum.
- NEUTRAL (45 <= Score <= 64):
    - VWAP_EMA: Only Volume Contact and Dry-Up Ignition setups allowed (high-conviction moves).
    - Momentum: Arming allowed.
- CHOPPY (Score < 45):
    - VWAP_EMA: Full standdown (all trade signals suppressed).
    - Momentum: Radar pre-alerts still fire (notifies of 25+ pt fast moves), but arming is SUPPRESSED.
"""

from collections import deque
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple
from loguru import logger


class RegimeFilter:
    """
    Session-level regime filter calculating a composite 4-component score
    to classify market state into TRENDING_BULL, TRENDING_BEAR, NEUTRAL, or CHOPPY.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.trending_threshold = float(cfg.get("trending_threshold", 65.0))
        self.neutral_threshold = float(cfg.get("neutral_threshold", 45.0))

        # Lookback parameters
        self.er_lookback = int(cfg.get("er_lookback", 10))
        self.vwap_persistence_lookback = int(cfg.get("vwap_persistence_lookback", 10))
        self.vwap_slope_lookback = int(cfg.get("vwap_slope_lookback", 5))

        # Scaling targets for VWAP slope (pts/bar on 3-min timeframe)
        self.nifty_target_slope = float(cfg.get("nifty_target_slope", 0.50))
        self.sensex_target_slope = float(cfg.get("sensex_target_slope", 1.50))

        # Component weights (sum = 1.00)
        self.w_adx = float(cfg.get("weight_adx", 0.30))
        self.w_er = float(cfg.get("weight_er", 0.30))
        self.w_persistence = float(cfg.get("weight_persistence", 0.25))
        self.w_slope = float(cfg.get("weight_slope", 0.15))

        # --- Hysteresis (anti-thrash) ---
        # Sep 29 produced 305 regime transitions on a choppy day because the raw score
        # crossing a threshold flipped the regime on a single candle. Hysteresis adds
        # (a) a deadband around each threshold so boundary jitter cannot flip state, and
        # (b) an N-bar confirmation so a candidate regime must persist before it commits.
        self.hysteresis_enabled = bool(cfg.get("hysteresis_enabled", True))
        self.trending_exit_band = float(cfg.get("trending_exit_band", 5.0))
        self.choppy_exit_band = float(cfg.get("choppy_exit_band", 5.0))
        self.confirm_bars_trending = int(cfg.get("confirm_bars_trending", 2))
        self.confirm_bars_relax = int(cfg.get("confirm_bars_relax", 1))
        self.direction_min_slope = float(cfg.get("direction_min_slope", 0.05))

        # --- Session net-change anchor ---
        # The BULL/BEAR label used to be built purely from VWAP slope + which side of VWAP
        # price sits, with NO awareness of the day's net move. That let a red day (gapped
        # down, grinding up off the lows) read TRENDING_BULL. We now reconcile the direction
        # against the session net-change: a directional trend label must AGREE with the sign
        # of net-change (vs the reference: previous close if known, else session open), once
        # net-change is meaningful (beyond a small deadband). If VWAP-derived direction
        # conflicts with a clear net-change, we demote the regime to NEUTRAL rather than
        # stamp a misleading trend.
        self.net_change_anchor_enabled = bool(cfg.get("net_change_anchor_enabled", True))
        # Net-change magnitude (as % of price) below which we don't enforce the anchor —
        # a nearly-flat day has no meaningful bias to enforce.
        self.net_change_deadband_pct = float(cfg.get("net_change_deadband_pct", 0.10))

        # Session reference per instrument: prev close, session open, latest ltp.
        self._session_ref: Dict[str, Dict[str, Any]] = {
            "NIFTY": {"prev_close": 0.0, "session_open": 0.0, "ltp": 0.0},
            "SENSEX": {"prev_close": 0.0, "session_open": 0.0, "ltp": 0.0},
        }

        # Pending-candidate tracker per instrument for confirmation-bar counting.
        # { inst: {"candidate": <regime str>, "count": <int>} }
        self._pending_change: Dict[str, Dict[str, Any]] = {
            "NIFTY": {"candidate": None, "count": 0},
            "SENSEX": {"candidate": None, "count": 0}
        }

        # History per instrument: "NIFTY" and "SENSEX"
        self.candle_history: Dict[str, deque] = {
            "NIFTY": deque(maxlen=30),
            "SENSEX": deque(maxlen=30)
        }
        self.vwap_history: Dict[str, deque] = {
            "NIFTY": deque(maxlen=30),
            "SENSEX": deque(maxlen=30)
        }
        self.adx_history: Dict[str, deque] = {
            "NIFTY": deque(maxlen=30),
            "SENSEX": deque(maxlen=30)
        }

        # Latest computed regime state
        self.latest_regime: Dict[str, Dict[str, Any]] = {
            "NIFTY": self._default_regime_state("NIFTY"),
            "SENSEX": self._default_regime_state("SENSEX")
        }

    def _default_regime_state(self, inst: str) -> Dict[str, Any]:
        return {
            "instrument": inst,
            "regime": "NEUTRAL",
            "direction": "NEUTRAL",
            "score": 50.0,
            "adx_score": 50.0,
            "er_score": 50.0,
            "persistence_score": 50.0,
            "slope_score": 50.0,
            "adx_value": 20.0,
            "er_value": 0.50,
            "vwap_slope": 0.0,
            "dominant_side": "NEUTRAL",
            "vwap_crosses": 0,
            "is_trending": False,
            "is_neutral": True,
            "is_choppy": False,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }

    def update_session_reference(self, inst: str, ltp: float, ts: float, prev_close: float = 0.0):
        """
        Records the session reference for the net-change anchor. Called on every spot tick
        by the engine's regime driver.
          - prev_close: previous session close (from the feed) when available; used as the
            primary net-change reference.
          - session_open: the first spot ltp seen this session (fallback reference / gap gauge).
          - ltp: latest spot price.
        Resets automatically at the start of a new IST trading day.
        """
        ref = self._session_ref.get(inst)
        if ref is None:
            return
        # Reset session open on a new day (by IST date).
        try:
            cur_date = self.parse_ist_time(ts).date()
        except Exception:
            cur_date = None
        if ref.get("date") != cur_date:
            ref["date"] = cur_date
            ref["session_open"] = ltp
            ref["prev_close"] = 0.0
        if ref.get("session_open", 0.0) <= 0.0:
            ref["session_open"] = ltp
        if prev_close and prev_close > 0.0:
            ref["prev_close"] = prev_close
        ref["ltp"] = ltp

    def parse_ist_time(self, ts: float):
        """Converts a unix timestamp to IST (mirrors BaseStrategy for standalone use)."""
        from datetime import timedelta
        ist = timezone(timedelta(hours=5, minutes=30))
        return datetime.fromtimestamp(ts, tz=ist)

    def _net_change_bias(self, inst: str) -> Tuple[str, float]:
        """
        Returns (bias, net_pct) where bias is BULL / BEAR / NEUTRAL based on the session
        net-change vs the reference (previous close preferred, else session open). NEUTRAL
        when net-change is within the deadband or no reference is available.
        """
        ref = self._session_ref.get(inst)
        if not ref:
            return "NEUTRAL", 0.0
        ltp = ref.get("ltp", 0.0)
        base = ref.get("prev_close", 0.0) or ref.get("session_open", 0.0)
        if ltp <= 0.0 or base <= 0.0:
            return "NEUTRAL", 0.0
        net_pct = (ltp - base) / base * 100.0
        if abs(net_pct) < self.net_change_deadband_pct:
            return "NEUTRAL", net_pct
        return ("BULL" if net_pct > 0 else "BEAR"), net_pct

    def calculate_adx_score(self, current_adx: float, prev_adx: Optional[float] = None) -> float:
        """
        Calculates ADX strength score (0 to 100) with bonus for rising ADX.
        Trend power gauge:
        - ADX < 15: 0 to 30
        - 15 <= ADX < 25: 30 to 65
        - 25 <= ADX < 40: 65 to 90
        - ADX >= 40: 90 to 100
        - Rising ADX bonus: +8 to +15 pts
        """
        if current_adx <= 15.0:
            base = (max(0.0, current_adx) / 15.0) * 30.0
        elif current_adx <= 25.0:
            base = 30.0 + ((current_adx - 15.0) / 10.0) * 35.0
        elif current_adx <= 40.0:
            base = 65.0 + ((current_adx - 25.0) / 15.0) * 25.0
        else:
            base = 90.0 + min(10.0, (current_adx - 40.0))

        bonus = 0.0
        if prev_adx is not None:
            diff = current_adx - prev_adx
            if diff > 0.1:
                # Rising ADX: trend is accelerating
                bonus = min(15.0, 8.0 + (diff * 5.0))
            elif diff < -0.2:
                # Falling ADX: trend is fading
                bonus = -5.0

        return max(0.0, min(100.0, base + bonus))

    def calculate_efficiency_ratio_score(self, closes: List[float]) -> Tuple[float, float]:
        """
        Calculates Kaufman Efficiency Ratio (ER) and its normalized score (0 to 100).
        ER = |Price_t - Price_{t-N}| / sum(|Price_i - Price_{i-1}|)
        ER near 1.0 = clean straight-line trend.
        ER near 0.0 = wandering erratic chop.
        """
        if len(closes) < 3:
            return 0.50, 50.0

        net_change = abs(closes[-1] - closes[0])
        sum_changes = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))

        if sum_changes <= 1e-5:
            return 0.0, 0.0

        er = max(0.0, min(1.0, net_change / sum_changes))

        # Piecewise mapping:
        # ER < 0.20: 0 to 20 (Severe chop)
        # 0.20 <= ER < 0.40: 20 to 50 (Weak/moderate wandering)
        # 0.40 <= ER < 0.70: 50 to 85 (Clean directional trending)
        # ER >= 0.70: 85 to 100 (Strong vertical impulse)
        if er < 0.20:
            er_score = (er / 0.20) * 20.0
        elif er < 0.40:
            er_score = 20.0 + ((er - 0.20) / 0.20) * 30.0
        elif er < 0.70:
            er_score = 50.0 + ((er - 0.40) / 0.30) * 35.0
        else:
            er_score = 85.0 + min(15.0, ((er - 0.70) / 0.30) * 15.0)

        return er, max(0.0, min(100.0, er_score))

    def calculate_vwap_persistence_score(
        self, candles: List[Dict[str, Any]], vwaps: List[float]
    ) -> Tuple[float, str, int]:
        """
        Calculates VWAP persistence score (0 to 100):
        Measures consistency of candle closes remaining on one side of VWAP across the lookback.
        - Dominant side ratio (0.5 to 1.0 mapped to 0 to 100)
        - Penalty for frequent crosses back and forth (whipsaw / oscillation)
        """
        n = min(len(candles), len(vwaps))
        if n < 3:
            return 50.0, "NEUTRAL", 0

        above = 0
        below = 0
        sides = []

        for i in range(-n, 0):
            c_close = candles[i]["close"]
            v_val = vwaps[i]
            if c_close >= v_val:
                above += 1
                sides.append(1)
            else:
                below += 1
                sides.append(-1)

        dominant = max(above, below)
        dom_side = "ABOVE" if above >= below else "BELOW"

        # Count crosses
        crosses = sum(1 for i in range(1, len(sides)) if sides[i] != sides[i - 1])

        # Base persistence: 50% dominant = 0 pts; 100% dominant = 100 pts
        dom_ratio = dominant / n
        raw_pers = max(0.0, (dom_ratio - 0.5) / 0.5 * 100.0)

        # Cross penalty: >= 3 crosses across 10 candles means active chop
        cross_penalty = 0.0
        if crosses >= 3:
            cross_penalty = min(30.0, (crosses - 2) * 10.0)

        pers_score = max(0.0, min(100.0, raw_pers - cross_penalty))
        return pers_score, dom_side, crosses

    def calculate_vwap_slope_score(
        self, vwaps: List[float], target_slope: float
    ) -> Tuple[float, float]:
        """
        Calculates rate of VWAP directional drift and scales to score (0 to 100).
        Slope = (VWAP_t - VWAP_{t-k}) / k (points per bar).
        """
        if len(vwaps) < 2:
            return 0.0, 50.0

        k = max(1, len(vwaps) - 1)
        slope = (vwaps[-1] - vwaps[0]) / k
        abs_slope = abs(slope)

        scaled_score = min(100.0, (abs_slope / max(0.01, target_slope)) * 100.0)
        return slope, scaled_score

    def _regime_family(self, regime: str) -> str:
        """Collapse TRENDING_BULL / TRENDING_BEAR to the 'TRENDING' family for band logic."""
        if "TRENDING" in regime:
            return "TRENDING"
        return regime  # "NEUTRAL" or "CHOPPY"

    def _raw_regime_with_deadband(self, score: float, prior_regime: str) -> str:
        """
        Maps the composite score to a raw regime *family* (TRENDING / NEUTRAL / CHOPPY)
        applying deadband hysteresis around each threshold.

        Enter TRENDING at score >= trending_threshold; only LEAVE it once the score drops
        below (trending_threshold - trending_exit_band). Symmetrically, enter CHOPPY at
        score < neutral_threshold but only LEAVE it once score rises above
        (neutral_threshold + choppy_exit_band). This kills boundary jitter (e.g. 64/66/64).
        """
        prior_family = self._regime_family(prior_regime)

        if not self.hysteresis_enabled:
            if score >= self.trending_threshold:
                return "TRENDING"
            if score >= self.neutral_threshold:
                return "NEUTRAL"
            return "CHOPPY"

        trend_exit = self.trending_threshold - self.trending_exit_band
        choppy_exit = self.neutral_threshold + self.choppy_exit_band

        if prior_family == "TRENDING":
            # Stay trending unless we fall clearly below the exit band.
            if score >= trend_exit:
                return "TRENDING"
            # Dropped out of trending — is it neutral or all the way to choppy?
            return "NEUTRAL" if score >= self.neutral_threshold else "CHOPPY"

        if prior_family == "CHOPPY":
            # Stay choppy unless we rise clearly above the exit band.
            if score < choppy_exit:
                return "CHOPPY"
            # Climbed out of chop — neutral or straight to trending?
            return "TRENDING" if score >= self.trending_threshold else "NEUTRAL"

        # prior was NEUTRAL: standard thresholds decide the candidate.
        if score >= self.trending_threshold:
            return "TRENDING"
        if score < self.neutral_threshold:
            return "CHOPPY"
        return "NEUTRAL"

    def _resolve_direction(
        self,
        raw_family: str,
        vwap_slope: float,
        dom_side: str,
        curr_close: float,
        curr_vwap: float
    ) -> str:
        """
        Resolves BULL / BEAR / NEUTRAL for a candidate TRENDING regime.

        Direction is only meaningful when trending. It must be corroborated by the VWAP
        slope sign (the drift of value) AND the persistence dominant side (where closes
        actually sat). If slope and persistence disagree, or slope is flatter than
        direction_min_slope, we do NOT stamp a directional trend — this is what prevents
        the false 'TREND UP' badge on a flat/choppy day.
        """
        if raw_family != "TRENDING":
            return "NEUTRAL"

        slope_dir = "NEUTRAL"
        if vwap_slope > self.direction_min_slope:
            slope_dir = "BULL"
        elif vwap_slope < -self.direction_min_slope:
            slope_dir = "BEAR"

        pers_dir = "NEUTRAL"
        if dom_side == "ABOVE":
            pers_dir = "BULL"
        elif dom_side == "BELOW":
            pers_dir = "BEAR"

        # Both signals agree: confident direction.
        if slope_dir != "NEUTRAL" and slope_dir == pers_dir:
            return slope_dir

        # Slope is decisive but persistence is neutral/unclear: trust the slope.
        if slope_dir != "NEUTRAL" and pers_dir == "NEUTRAL":
            return slope_dir

        # Slope flat but persistence clear: trust persistence (slow grind trend).
        if slope_dir == "NEUTRAL" and pers_dir != "NEUTRAL":
            return pers_dir

        # Slope and persistence actively CONFLICT (e.g. slope up but closes mostly below
        # VWAP) — this is chop masquerading as trend. Fall back to the raw close-vs-VWAP
        # only if slope is non-trivial; otherwise NEUTRAL.
        if slope_dir != "NEUTRAL":
            return slope_dir  # slope wins ties over stale persistence
        return "NEUTRAL"

    def _apply_hysteresis(
        self,
        inst: str,
        candidate: str,
        candidate_direction: str,
        prior_regime: str,
        prior_state: Dict[str, Any]
    ) -> Tuple[str, str]:
        """
        Confirmation-bar gate. A candidate regime must persist for the required number of
        consecutive candles before it is committed. Moving INTO trending needs
        confirm_bars_trending bars; relaxing toward NEUTRAL/CHOPPY needs confirm_bars_relax.

        Returns the (committed_regime, committed_direction).
        """
        if not self.hysteresis_enabled:
            return candidate, candidate_direction

        prior_direction = prior_state.get("direction", "NEUTRAL")

        # No change candidate == prior: reset pending, keep prior. (Direction may still
        # refine within the same trending family, so allow same-family direction update.)
        if candidate == prior_regime:
            self._pending_change[inst] = {"candidate": None, "count": 0}
            return candidate, candidate_direction

        # Same TRENDING family but only the direction label changed. Treat a direction flip
        # (BULL<->BEAR) as a change needing trending confirmation; keep prior until confirmed.
        prior_family = self._regime_family(prior_regime)
        cand_family = self._regime_family(candidate)

        # Determine how many confirmation bars this transition needs.
        if cand_family == "TRENDING":
            required = self.confirm_bars_trending
        else:
            required = self.confirm_bars_relax

        pend = self._pending_change.get(inst, {"candidate": None, "count": 0})
        if pend.get("candidate") == candidate:
            pend["count"] += 1
        else:
            pend = {"candidate": candidate, "count": 1}

        if pend["count"] >= required:
            # Commit the change; clear pending.
            self._pending_change[inst] = {"candidate": None, "count": 0}
            return candidate, candidate_direction

        # Not yet confirmed: hold the prior regime, but let the pending counter ride.
        self._pending_change[inst] = pend
        return prior_regime, prior_direction

    def update_candle(
        self,
        inst: str,
        candle: Dict[str, Any],
        vwap: float,
        adx: float
    ) -> Dict[str, Any]:
        """
        Updates regime state when a candle (e.g. 3-minute bar) closes.
        """
        if not self.enabled:
            return self.latest_regime.get(inst, self._default_regime_state(inst))

        candles = self.candle_history[inst]
        vwaps = self.vwap_history[inst]
        adxs = self.adx_history[inst]

        prev_adx = adxs[-1] if len(adxs) > 0 else None

        candles.append(candle)
        vwaps.append(vwap)
        adxs.append(adx)

        # 1. ADX Strength Component (30%)
        adx_score = self.calculate_adx_score(adx, prev_adx)

        # 2. Efficiency Ratio Component (30%)
        er_window_closes = [c["close"] for c in list(candles)[-self.er_lookback:]]
        er_val, er_score = self.calculate_efficiency_ratio_score(er_window_closes)

        # 3. VWAP Persistence Component (25%)
        pers_candles = list(candles)[-self.vwap_persistence_lookback:]
        pers_vwaps = list(vwaps)[-self.vwap_persistence_lookback:]
        pers_score, dom_side, crosses = self.calculate_vwap_persistence_score(pers_candles, pers_vwaps)

        # 4. VWAP Slope Component (15%)
        target_slope = self.nifty_target_slope if inst == "NIFTY" else self.sensex_target_slope
        slope_vwaps = list(vwaps)[-self.vwap_slope_lookback:]
        vwap_slope, slope_score = self.calculate_vwap_slope_score(slope_vwaps, target_slope)

        # Composite 4-Component Score
        composite_score = (
            (self.w_adx * adx_score) +
            (self.w_er * er_score) +
            (self.w_persistence * pers_score) +
            (self.w_slope * slope_score)
        )
        composite_score = max(0.0, min(100.0, composite_score))

        # Regime and Direction Classification
        curr_close = float(candle.get("close", 0.0))
        curr_vwap = float(vwap)

        prior = self.latest_regime.get(inst, self._default_regime_state(inst))
        prior_regime = prior.get("regime", "NEUTRAL")

        # 1. Raw candidate regime from the current score, using deadband bands so a score
        #    hovering at a boundary does not flip the committed regime back and forth.
        raw_regime = self._raw_regime_with_deadband(composite_score, prior_regime)

        # 2. Direction only matters for a TRENDING regime, and must be corroborated by the
        #    VWAP slope sign and the persistence dominant side, not an instantaneous
        #    close-vs-VWAP snapshot (which is what mislabeled Sep 29 as "TREND UP").
        direction = self._resolve_direction(raw_regime, vwap_slope, dom_side, curr_close, curr_vwap)
        if raw_regime == "TRENDING":
            if direction == "NEUTRAL":
                # High score but no corroborated direction (flat slope + no dominant side):
                # this is not a real trend. Demote to NEUTRAL rather than stamp a false trend.
                raw_regime = "NEUTRAL"
            else:
                # Session net-change anchor: a directional trend must not fight the day's
                # actual net move. If VWAP-derived direction conflicts with a clear
                # net-change bias (e.g. slope says BULL but the day is clearly red), this is
                # a counter-net-change reading — demote to NEUTRAL instead of a false trend.
                if self.net_change_anchor_enabled:
                    bias, net_pct = self._net_change_bias(inst)
                    if bias != "NEUTRAL" and bias != direction:
                        logger.info(
                            f"🧭 [REGIME NET-CHANGE ANCHOR] {inst} demoting TRENDING_{direction} → NEUTRAL: "
                            f"conflicts with session net-change {net_pct:+.2f}% ({bias})"
                        )
                        raw_regime = "NEUTRAL"
                        direction = "NEUTRAL"
                    else:
                        raw_regime = f"TRENDING_{direction}"
                else:
                    raw_regime = f"TRENDING_{direction}"

        # 3. Hysteresis confirmation: require the candidate to persist for N bars before
        #    committing, so single-bar spikes cannot stamp a new regime.
        regime, direction = self._apply_hysteresis(inst, raw_regime, direction, prior_regime, prior)

        state = {
            "instrument": inst,
            "regime": regime,
            "direction": direction,
            "score": round(composite_score, 1),
            "adx_score": round(adx_score, 1),
            "er_score": round(er_score, 1),
            "persistence_score": round(pers_score, 1),
            "slope_score": round(slope_score, 1),
            "adx_value": round(adx, 2),
            "er_value": round(er_val, 3),
            "vwap_slope": round(vwap_slope, 2),
            "dominant_side": dom_side,
            "vwap_crosses": crosses,
            "is_trending": "TRENDING" in regime,
            "is_neutral": regime == "NEUTRAL",
            "is_choppy": regime == "CHOPPY",
            "updated_at": datetime.now(timezone.utc).isoformat()
        }

        # Log transition if changed
        prior = self.latest_regime.get(inst, {})
        if prior.get("regime") != regime:
            logger.info(
                f"🌐 [REGIME TRANSITION] {inst} ➔ {regime} (Score: {composite_score:.1f} | "
                f"ADX: {adx:.1f}, ER: {er_val:.2f}, Pers: {pers_score:.0f}, Slope: {vwap_slope:+.2f})"
            )

        self.latest_regime[inst] = state
        return state

    def get_regime(self, inst: str) -> Dict[str, Any]:
        """Returns the latest regime state dictionary for the instrument."""
        if not self.enabled:
            default = self._default_regime_state(inst)
            default["regime"] = "TRENDING_BULL"  # If filter disabled, allow everything
            default["score"] = 100.0
            return default
        return self.latest_regime.get(inst, self._default_regime_state(inst))

    def allows_vwap_ema(
        self,
        inst: str,
        is_volume_contact: bool = False,
        is_dryup_ignition: bool = False
    ) -> Tuple[bool, str]:
        """
        Evaluates whether VWAP_EMA setup is permitted:
        - TRENDING_BULL / TRENDING_BEAR (>= 65): All setups allowed.
        - NEUTRAL (45-64): ONLY Volume Contact and Dry-Up Ignition allowed.
        - CHOPPY (< 45): Full standdown (all setups blocked).
        """
        if not self.enabled:
            return True, "RegimeFilter disabled"

        st = self.get_regime(inst)
        regime = st.get("regime", "NEUTRAL")
        score = st.get("score", 50.0)

        if "TRENDING" in regime:
            return True, f"All setups allowed in {regime} (Score: {score:.1f})"

        if regime == "NEUTRAL":
            if is_volume_contact or is_dryup_ignition:
                setup_desc = "Volume Contact" if is_volume_contact else "Dry-Up Ignition"
                return True, f"High-conviction {setup_desc} allowed in NEUTRAL (Score: {score:.1f})"
            return False, f"Standard setup suppressed in NEUTRAL (Score: {score:.1f}) — requires Volume Contact or Dry-Up Ignition"

        # CHOPPY
        return False, f"VWAP_EMA full standdown: regime is CHOPPY (Score: {score:.1f} < {self.neutral_threshold})"

    def allows_momentum_arming(self, inst: str) -> Tuple[bool, str]:
        """
        Evaluates whether Momentum Impulse arming (trade entry state) is permitted:
        - TRENDING & NEUTRAL: Allowed.
        - CHOPPY (< 45): SUPPRESSED to prevent entering false impulses that reverse in chop.
        """
        if not self.enabled:
            return True, "RegimeFilter disabled"

        st = self.get_regime(inst)
        regime = st.get("regime", "NEUTRAL")
        score = st.get("score", 50.0)

        if regime == "CHOPPY":
            return False, f"Momentum arming suppressed: regime is CHOPPY (Score: {score:.1f} < {self.neutral_threshold})"

        return True, f"Momentum arming allowed in {regime} (Score: {score:.1f})"

    def allows_momentum_radar(self, inst: str) -> Tuple[bool, str]:
        """
        Radar pre-alerts ALWAYS fire (even in CHOPPY) so the trader
        maintains visibility on fast 25+ pt price surges.
        """
        return True, "Radar pre-alerts permitted across all regimes"

    def allows_oi_squeeze(self, inst: str, direction: str) -> Tuple[bool, str]:
        """
        Evaluates whether an OI Squeeze signal in the given direction (CE/PE) is permitted.

        The Sep 29 recorded-tick backtest showed OI Squeeze bleeds on counter-trend CE
        spikes: on a bearish/choppy day it repeatedly bought call spikes that mean-reverted
        (SENSEX: 6 CE trades -₹1,488 vs 3 PE trades +₹1,968). So:
          - CHOPPY (< 45): full standdown — a price spike + OI unwind in chop is usually noise.
          - TRENDING_BULL: allow CE only (block counter-trend PE).
          - TRENDING_BEAR: allow PE only (block counter-trend CE).
          - NEUTRAL: allow both — the middle ground; the directional gate only bites in the
            clearer TRENDING regime where the counter-trend losses actually occurred.
        """
        if not self.enabled:
            return True, "RegimeFilter disabled"

        st = self.get_regime(inst)
        regime = st.get("regime", "NEUTRAL")
        score = st.get("score", 50.0)

        if regime == "CHOPPY":
            return False, f"OI Squeeze standdown: regime is CHOPPY (Score: {score:.1f} < {self.neutral_threshold})"

        if regime == "TRENDING_BULL" and direction == "PE":
            return False, f"OI Squeeze PE blocked: counter-trend in TRENDING_BULL (Score: {score:.1f})"

        if regime == "TRENDING_BEAR" and direction == "CE":
            return False, f"OI Squeeze CE blocked: counter-trend in TRENDING_BEAR (Score: {score:.1f})"

        return True, f"OI Squeeze {direction} allowed in {regime} (Score: {score:.1f})"

    def seed_from_candles(self, inst: str, candles: List[Dict[str, Any]]):
        """
        Feeds historical candles to warm up the regime filter on startup.
        Computes rolling VWAP and ADX on the historical candles.
        """
        if not candles:
            return

        from backend.strategies.indicators import IncrementalADX, IncrementalVWAP

        adx_ind = IncrementalADX(period=14)
        vwap_ind = IncrementalVWAP()

        # Clear any pending hysteresis candidate so warmup starts from a clean slate.
        self._pending_change[inst] = {"candidate": None, "count": 0}

        # Seed the session net-change reference from the warm-up candles: the last candle's
        # close is the best available "previous close" proxy until the live feed provides one.
        ref = self._session_ref.get(inst)
        if ref is not None and candles:
            last_close = float(candles[-1].get("close", 0.0) or 0.0)
            if last_close > 0.0:
                ref["prev_close"] = last_close
                ref["ltp"] = last_close

        logger.info(f"🔄 [REGIME_FILTER] Warming up {inst} with {len(candles)} historical candles...")

        for c in candles:
            h = float(c.get("high", 0.0))
            l = float(c.get("low", 0.0))
            cl = float(c.get("close", 0.0))
            v = float(c.get("volume", 1000.0))
            if cl <= 0:
                continue

            typical_price = (h + l + cl) / 3.0
            v_val = vwap_ind.update(typical_price, v)
            a_val = adx_ind.update(h, l, cl)
            self.update_candle(inst, c, v_val, a_val)

        final_st = self.get_regime(inst)
        logger.success(
            f"✅ [REGIME_FILTER] {inst} Warmup Complete: Regime = {final_st['regime']} "
            f"(Score: {final_st['score']} | ADX: {final_st['adx_value']}, ER: {final_st['er_value']}, "
            f"Pers: {final_st['persistence_score']}, Slope: {final_st['vwap_slope']:+.2f})"
        )

    def reset_session(self, inst: str):
        """
        Clears all session-scoped state for an instrument so the regime starts
        clean for the next trading day. Called by the EOD sweep when the app runs
        continuously across sessions without a restart.

        Resets:
          - Latest committed regime → default (NEUTRAL, score 50)
          - Hysteresis pending candidate → empty
          - Session net-change anchor (prev_close, session_open, ltp) → zeroed
          - Candle / VWAP / ADX history deques → cleared
        """
        self.latest_regime[inst] = self._default_regime_state(inst)
        self._pending_change[inst] = {"candidate": None, "count": 0}
        self._session_ref[inst] = {"prev_close": 0.0, "session_open": 0.0, "ltp": 0.0}
        if inst in self.candle_history:
            self.candle_history[inst].clear()
        if inst in self.vwap_history:
            self.vwap_history[inst].clear()
        if inst in self.adx_history:
            self.adx_history[inst].clear()
        logger.info(f"🔄 [REGIME_FILTER] {inst} session state reset for next trading day.")

