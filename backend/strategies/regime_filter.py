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

        if composite_score >= self.trending_threshold:
            # Bullish vs Bearish alignment
            if curr_close >= curr_vwap and vwap_slope >= -0.05:
                regime = "TRENDING_BULL"
                direction = "BULL"
            elif curr_close <= curr_vwap and vwap_slope <= 0.05:
                regime = "TRENDING_BEAR"
                direction = "BEAR"
            else:
                direction = "BULL" if curr_close >= curr_vwap else "BEAR"
                regime = f"TRENDING_{direction}"
        elif composite_score >= self.neutral_threshold:
            regime = "NEUTRAL"
            direction = "NEUTRAL"
        else:
            regime = "CHOPPY"
            direction = "NEUTRAL"

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
