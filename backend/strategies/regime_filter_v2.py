"""
PROJECT ALPHA 3.0 — REGIME FILTER v2  (A/B candidate, non-gating until validated)
=================================================================================
Improves on v1 per the architecture review:

  TREND QUALITY (how strongly/cleanly trending — a 0..100 score)
    - ADX strength (with rising-ADX bonus)
    - Efficiency Ratio (Kaufman): clean trend vs chop
    - VWAP persistence: now DISTANCE-weighted (how far, not just which side) + cross penalty
    - VWAP slope: now ATR-NORMALIZED (volatility-adjusted, comparable across days)

  DIRECTION (decoupled from strength — not forced onto the same variables)
    - DI+/DI- (primary directional evidence)
    - price vs VWAP, VWAP slope sign
    - market breadth sign (when available)

  PARTICIPATION (market breadth — LIVE ONLY, cannot be backtested)
    - breadth % + weighted pressure from index heavyweights
    - adjusts score up (confirmation) / down (divergence) and can veto direction

  STATES (6): TRENDING_BULL / TRENDING_BEAR (HEALTHY vs WEAKENING via slope fade),
              NEUTRAL, CHOPPY.

HONESTY: weights/thresholds are an engineering starting point, NOT statistically validated.
The score is NOT a probability. Participation is live-only (no recorded constituent history),
so only trend-quality can be backtested. Ships non-gating; validate before any strategy consumes.
"""
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple
from loguru import logger


class RegimeFilterV2:
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))

        # Score thresholds (unvalidated starting points — calibrate against outcomes later).
        self.trending_threshold = float(cfg.get("trending_threshold", 65.0))
        self.neutral_threshold = float(cfg.get("neutral_threshold", 45.0))

        # Trend-quality component weights (sum = 1.0). Direction handled SEPARATELY.
        self.w_adx = float(cfg.get("weight_adx", 0.30))
        self.w_er = float(cfg.get("weight_er", 0.30))
        self.w_persistence = float(cfg.get("weight_persistence", 0.25))
        self.w_slope = float(cfg.get("weight_slope", 0.15))

        # Lookbacks
        self.er_lookback = int(cfg.get("er_lookback", 10))
        self.persistence_lookback = int(cfg.get("vwap_persistence_lookback", 10))
        self.slope_lookback = int(cfg.get("vwap_slope_lookback", 5))

        # ATR-normalized slope target (slope-per-bar / ATR-per-bar). ~0.15 means VWAP drifting
        # ~15% of an ATR per bar is a "full" slope score. Tunable.
        self.slope_atr_target = float(cfg.get("slope_atr_target", 0.15))

        # Participation (breadth) influence. 0 disables it (e.g. for backtests with no breadth).
        self.use_participation = bool(cfg.get("use_participation", True))
        self.participation_weight = float(cfg.get("participation_weight", 0.20))  # +/- adj to score
        # Slope-fade threshold (as fraction of ATR-normalized slope) for HEALTHY vs WEAKENING.
        self.weakening_slope_frac = float(cfg.get("weakening_slope_frac", 0.05))

        self.candle_history: Dict[str, deque] = {"NIFTY": deque(maxlen=40), "SENSEX": deque(maxlen=40)}
        self.vwap_history: Dict[str, deque] = {"NIFTY": deque(maxlen=40), "SENSEX": deque(maxlen=40)}
        self.adx_history: Dict[str, deque] = {"NIFTY": deque(maxlen=40), "SENSEX": deque(maxlen=40)}
        self.latest: Dict[str, Dict[str, Any]] = {
            "NIFTY": self._default_state("NIFTY"),
            "SENSEX": self._default_state("SENSEX"),
        }

    def _default_state(self, inst: str) -> Dict[str, Any]:
        return {
            "instrument": inst, "regime": "NEUTRAL", "direction": "NEUTRAL", "health": "NA",
            "score": 50.0, "trend_quality": 50.0, "participation_adj": 0.0,
            "adx_score": 50.0, "er_score": 50.0, "persistence_score": 50.0, "slope_score": 50.0,
            "adx_value": 20.0, "plus_di": 0.0, "minus_di": 0.0, "er_value": 0.5,
            "vwap_slope_norm": 0.0, "breadth_pct": None, "weighted_pressure": None,
            "is_trending": False, "is_neutral": True, "is_choppy": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    # ---- component scorers ----
    @staticmethod
    def _adx_score(adx: float, prev_adx: Optional[float]) -> float:
        if adx <= 15.0:
            base = max(0.0, adx) / 15.0 * 30.0
        elif adx <= 25.0:
            base = 30.0 + (adx - 15.0) / 10.0 * 35.0
        elif adx <= 40.0:
            base = 65.0 + (adx - 25.0) / 15.0 * 25.0
        else:
            base = 90.0 + min(10.0, adx - 40.0)
        bonus = 0.0
        if prev_adx is not None:
            d = adx - prev_adx
            bonus = min(15.0, 8.0 + d * 5.0) if d > 0.1 else (-5.0 if d < -0.2 else 0.0)
        return max(0.0, min(100.0, base + bonus))

    @staticmethod
    def _er_score(closes: List[float]) -> Tuple[float, float]:
        if len(closes) < 3:
            return 0.5, 50.0
        net = abs(closes[-1] - closes[0])
        tot = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
        if tot <= 1e-5:
            return 0.0, 0.0
        er = max(0.0, min(1.0, net / tot))
        if er < 0.20:
            s = er / 0.20 * 20.0
        elif er < 0.40:
            s = 20.0 + (er - 0.20) / 0.20 * 30.0
        elif er < 0.70:
            s = 50.0 + (er - 0.40) / 0.30 * 35.0
        else:
            s = 85.0 + min(15.0, (er - 0.70) / 0.30 * 15.0)
        return er, max(0.0, min(100.0, s))

    def _persistence_score(self, candles: List[Dict[str, Any]], vwaps: List[float]) -> Tuple[float, str, int]:
        """DISTANCE-weighted persistence: reward closes that are both consistently on one side
        AND meaningfully far from VWAP, penalize frequent crosses."""
        n = min(len(candles), len(vwaps))
        if n < 3:
            return 50.0, "NEUTRAL", 0
        sides, norm_dists = [], []
        above = below = 0
        for i in range(-n, 0):
            c = candles[i]["close"]; v = vwaps[i]
            if v <= 0:
                continue
            side = 1 if c >= v else -1
            sides.append(side)
            above += side == 1
            below += side == -1
            # normalized distance from VWAP as % of price, capped so one spike can't dominate
            norm_dists.append(min(abs(c - v) / v, 0.01) / 0.01)  # 0..1 where 1% away = full
        if not sides:
            return 50.0, "NEUTRAL", 0
        dominant = max(above, below)
        dom_side = "ABOVE" if above >= below else "BELOW"
        dom_ratio = dominant / len(sides)
        avg_dist = sum(norm_dists) / len(norm_dists)
        # base: how one-sided (0.5->0, 1.0->100), scaled by how FAR price sits from VWAP
        raw = max(0.0, (dom_ratio - 0.5) / 0.5) * 100.0
        dist_scaled = raw * (0.5 + 0.5 * avg_dist)  # distance can halve or fully credit the persistence
        crosses = sum(1 for i in range(1, len(sides)) if sides[i] != sides[i - 1])
        penalty = min(30.0, (crosses - 2) * 10.0) if crosses >= 3 else 0.0
        return max(0.0, min(100.0, dist_scaled - penalty)), dom_side, crosses

    def _slope_score_norm(self, vwaps: List[float], atr: float) -> Tuple[float, float]:
        """ATR-normalized VWAP slope: (slope per bar) / (ATR per bar). Volatility-adjusted."""
        if len(vwaps) < 2 or atr <= 0:
            return 0.0, 50.0
        k = max(1, len(vwaps) - 1)
        slope = (vwaps[-1] - vwaps[0]) / k
        slope_norm = slope / atr  # fraction of an ATR per bar (signed)
        score = min(100.0, abs(slope_norm) / max(0.01, self.slope_atr_target) * 100.0)
        return slope_norm, score

    def update_candle(
        self, inst: str, candle: Dict[str, Any], vwap: float, adx: float,
        plus_di: float = 0.0, minus_di: float = 0.0, atr: float = 0.0,
        breadth: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not self.enabled:
            return self.latest.get(inst, self._default_state(inst))

        candles = self.candle_history[inst]; vwaps = self.vwap_history[inst]; adxs = self.adx_history[inst]
        prev_adx = adxs[-1] if adxs else None
        candles.append(candle); vwaps.append(vwap); adxs.append(adx)

        adx_score = self._adx_score(adx, prev_adx)
        er_val, er_score = self._er_score([c["close"] for c in list(candles)[-self.er_lookback:]])
        pers_score, dom_side, crosses = self._persistence_score(
            list(candles)[-self.persistence_lookback:], list(vwaps)[-self.persistence_lookback:])
        slope_norm, slope_score = self._slope_score_norm(list(vwaps)[-self.slope_lookback:], atr)

        trend_quality = max(0.0, min(100.0,
            self.w_adx * adx_score + self.w_er * er_score
            + self.w_persistence * pers_score + self.w_slope * slope_score))

        # ---- Participation adjustment (breadth) ----
        participation_adj = 0.0
        breadth_pct = weighted_pressure = None
        if self.use_participation and breadth and breadth.get("valid"):
            breadth_pct = breadth.get("breadth_pct")
            weighted_pressure = breadth.get("weighted_pressure_pct")
            # Confirmation: breadth far from 50% in the SAME direction as price adds; divergence subtracts.
            # Signed breadth bias: +1 strong-advancing, -1 strong-declining.
            b_bias = (breadth_pct - 0.5) * 2.0 if breadth_pct is not None else 0.0  # -1..+1
            curr_close = float(candle.get("close", 0.0)); curr_vwap = float(vwap)
            price_dir = 1 if curr_close >= curr_vwap else -1
            # agreement in [-1,1]: breadth bias aligned with price direction
            agreement = b_bias * price_dir
            participation_adj = agreement * (self.participation_weight * 100.0)  # +/- up to weight*100
            if breadth.get("spoof_detected"):
                participation_adj -= 10.0  # single-stock skew: trust the move less

        score = max(0.0, min(100.0, trend_quality + participation_adj))

        # ---- Direction (decoupled): DI first, then price-vs-VWAP + slope + breadth ----
        curr_close = float(candle.get("close", 0.0)); curr_vwap = float(vwap)
        di_dir = 0
        if plus_di or minus_di:
            di_dir = 1 if plus_di > minus_di else (-1 if minus_di > plus_di else 0)
        price_dir = 1 if curr_close >= curr_vwap else -1
        slope_dir = 1 if slope_norm > 0 else (-1 if slope_norm < 0 else 0)
        breadth_dir = 0
        if breadth_pct is not None:
            breadth_dir = 1 if breadth_pct >= 0.55 else (-1 if breadth_pct <= 0.45 else 0)
        # weighted vote
        vote = 2 * di_dir + 1.5 * price_dir + 1.0 * slope_dir + 1.0 * breadth_dir
        direction = "BULL" if vote > 0 else ("BEAR" if vote < 0 else ("BULL" if price_dir > 0 else "BEAR"))

        # ---- Classify ----
        if score >= self.trending_threshold:
            # HEALTHY vs WEAKENING: slope fading against the trend direction = weakening
            healthy = True
            if direction == "BULL" and slope_norm < self.weakening_slope_frac:
                healthy = False
            elif direction == "BEAR" and slope_norm > -self.weakening_slope_frac:
                healthy = False
            regime = f"TRENDING_{direction}"
            health = "HEALTHY" if healthy else "WEAKENING"
        elif score >= self.neutral_threshold:
            regime, direction, health = "NEUTRAL", "NEUTRAL", "NA"
        else:
            regime, direction, health = "CHOPPY", "NEUTRAL", "NA"

        state = {
            "instrument": inst, "regime": regime, "direction": direction, "health": health,
            "score": round(score, 1), "trend_quality": round(trend_quality, 1),
            "participation_adj": round(participation_adj, 1),
            "adx_score": round(adx_score, 1), "er_score": round(er_score, 1),
            "persistence_score": round(pers_score, 1), "slope_score": round(slope_score, 1),
            "adx_value": round(adx, 2), "plus_di": round(plus_di, 1), "minus_di": round(minus_di, 1),
            "er_value": round(er_val, 3), "vwap_slope_norm": round(slope_norm, 4),
            "dominant_side": dom_side, "vwap_crosses": crosses,
            "breadth_pct": breadth_pct, "weighted_pressure": weighted_pressure,
            "is_trending": "TRENDING" in regime, "is_neutral": regime == "NEUTRAL",
            "is_choppy": regime == "CHOPPY",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        prior = self.latest.get(inst, {})
        if prior.get("regime") != regime or prior.get("health") != health:
            logger.info(
                f"🧭 [REGIME v2] {inst} ➔ {regime}{'/' + health if health != 'NA' else ''} "
                f"(score {score:.1f} = TQ {trend_quality:.0f} {participation_adj:+.0f}part | "
                f"ADX {adx:.0f} DI+/- {plus_di:.0f}/{minus_di:.0f} ER {er_val:.2f} "
                f"slopeN {slope_norm:+.3f} breadth {breadth_pct})"
            )
        self.latest[inst] = state
        return state

    def get_regime(self, inst: str) -> Dict[str, Any]:
        if not self.enabled:
            d = self._default_state(inst); d["regime"] = "TRENDING_BULL"; d["score"] = 100.0
            return d
        return self.latest.get(inst, self._default_state(inst))
