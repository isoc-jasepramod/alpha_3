from collections import deque
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from loguru import logger
from backend.strategies.base_strategy import BaseStrategy
from backend.strategies.indicators import (
    IncrementalEMA,
    IncrementalVWAP,
    IncrementalRSI,
    IncrementalADX,
    CandleAggregator,
    calculate_bottom_wick_ratio,
    calculate_top_wick_ratio,
    VolumeContactDetector,
    VolumeDryUpDetector
)


class VWAPEMAAlignment(BaseStrategy):
    """
    VWAP & EMA Institutional Alignment (09:15 - 15:15)
    Evaluated strictly on 3-minute candles with ADX and VWAP Slope Gates.

    Anti-chop filters (new):
    1. Intraday Range Exhaustion: Suppresses trend-continuation signals when spot
       has already moved > 2× ATR from the day's high (PE) or low (CE).
    2. Consecutive Stop Cooldown: 15-min pause after 2 consecutive stops on same
       instrument + direction (inherited from BaseStrategy).
    3. VWAP Distance Filter: Rejects PE entries if spot is > 0.3% below VWAP
       (chasing, not pulling back), and CE entries if spot is > 0.3% above VWAP.

    CE Trigger:
      EMA9 > EMA21 and Close > VWAP.
      VWAP Slope >= +0.35 pt/bar and ADX >= 22.0.
      Candle low tests EMA9 or within 0.05% of VWAP.
      Bottom Wick Rejection Ratio >= 0.40.
      RSI(14) in [48, 72].
    PE Trigger:
      EMA9 < EMA21 and Close < VWAP.
      VWAP Slope <= -0.35 pt/bar and ADX >= 22.0.
      Candle high tests EMA9 or within 0.05% of VWAP.
      Top Wick Rejection Ratio >= 0.40.
      RSI(14) in [28, 52].
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(name="VWAP_EMA")
        cfg = config or {}
        self.start_time = cfg.get("start_time", "09:15:00")
        self.end_time = cfg.get("end_time", "15:15:00")
        self.vwap_tolerance = cfg.get("vwap_touch_tolerance", 0.0005)
        self.min_bottom_wick = cfg.get("min_bottom_wick_ratio", 0.40)
        self.min_top_wick = cfg.get("min_top_wick_ratio", 0.40)
        self.rsi_ce_min = cfg.get("rsi_ce_min", 48.0)
        self.rsi_ce_max = cfg.get("rsi_ce_max", 72.0)
        self.rsi_pe_min = cfg.get("rsi_pe_min", 18.0)
        self.rsi_pe_max = cfg.get("rsi_pe_max", 52.0)
        self.min_vol_ratio = cfg.get("min_vol_ratio", 1.20)
        self.min_adx = cfg.get("min_adx", 22.0)
        self.min_vwap_slope = cfg.get("min_vwap_slope", 0.35)

        # Anti-chop filter parameters
        self.exhaustion_atr_multiplier = cfg.get("exhaustion_atr_multiplier", 2.0)
        self.min_atr_floor = cfg.get("min_atr_floor", {"NIFTY": 30.0, "SENSEX": 80.0})
        self.max_vwap_distance_pct = cfg.get("max_vwap_distance_pct", 0.30)

        # 3-min candle aggregators on Spot
        self.spot_3m_aggregators: Dict[str, CandleAggregator] = {
            "NIFTY": CandleAggregator(timeframe_seconds=180),
            "SENSEX": CandleAggregator(timeframe_seconds=180)
        }
        self.spot_3m_vols: Dict[str, deque] = {
            "NIFTY": deque(maxlen=20),
            "SENSEX": deque(maxlen=20)
        }

        # Spot indicators
        self.spot_ema9: Dict[str, IncrementalEMA] = {
            "NIFTY": IncrementalEMA(period=9),
            "SENSEX": IncrementalEMA(period=9)
        }
        self.spot_ema21: Dict[str, IncrementalEMA] = {
            "NIFTY": IncrementalEMA(period=21),
            "SENSEX": IncrementalEMA(period=21)
        }
        self.spot_rsi: Dict[str, IncrementalRSI] = {
            "NIFTY": IncrementalRSI(period=14),
            "SENSEX": IncrementalRSI(period=14)
        }
        self.spot_vwap: Dict[str, IncrementalVWAP] = {
            "NIFTY": IncrementalVWAP(),
            "SENSEX": IncrementalVWAP()
        }
        self.spot_adx: Dict[str, IncrementalADX] = {
            "NIFTY": IncrementalADX(period=14),
            "SENSEX": IncrementalADX(period=14)
        }
        self.spot_vwap_history: Dict[str, deque] = {
            "NIFTY": deque(maxlen=5),
            "SENSEX": deque(maxlen=5)
        }

        # Volume dynamics detectors (Volume Contact & Dry-Up Ignition)
        self.contact_detectors: Dict[str, VolumeContactDetector] = {
            "NIFTY": VolumeContactDetector(baseline_period=15, contact_multiplier=cfg.get("contact_multiplier", 1.8), proximity_pct=self.vwap_tolerance),
            "SENSEX": VolumeContactDetector(baseline_period=15, contact_multiplier=cfg.get("contact_multiplier", 1.8), proximity_pct=self.vwap_tolerance)
        }
        self.dryup_detectors: Dict[str, VolumeDryUpDetector] = {
            "NIFTY": VolumeDryUpDetector(lookback_period=15, min_dry_bars=cfg.get("min_dry_bars", 3), ignition_multiplier=cfg.get("ignition_multiplier", 2.0)),
            "SENSEX": VolumeDryUpDetector(lookback_period=15, min_dry_bars=cfg.get("min_dry_bars", 3), ignition_multiplier=cfg.get("ignition_multiplier", 2.0))
        }


        # Intraday high/low tracking for exhaustion filter
        self.day_high: Dict[str, float] = {"NIFTY": -1.0, "SENSEX": -1.0}
        self.day_low: Dict[str, float] = {"NIFTY": 1e9, "SENSEX": 1e9}
        self.spot_atr_value: Dict[str, float] = {"NIFTY": 0.0, "SENSEX": 0.0}

        # Confirmation candle state: candle 1 detected, waiting for candle 2 to confirm bounce
        self.awaiting_confirmation: Dict[str, Optional[Dict[str, Any]]] = {
            "NIFTY": None,
            "SENSEX": None
        }

        self.pending_signal: Dict[str, Optional[Dict[str, Any]]] = {
            "NIFTY": None,
            "SENSEX": None
        }

    def seed_from_spot(self, inst: str, spot_info: Dict[str, Any]):
        ltp = float(spot_info.get("ltp", 0.0))
        if ltp <= 0:
            return

        op = float(spot_info.get("open") or ltp)
        hi = float(spot_info.get("high") or ltp)
        lo = float(spot_info.get("low") or ltp)

        # Sanity check: if open, high, low are 0 or unreasonable, default to ltp
        if op <= 0 or abs(op - ltp) / ltp > 0.08:
            op = ltp
        if hi <= 0 or hi < ltp or abs(hi - ltp) / ltp > 0.08:
            hi = max(ltp, op)
        if lo <= 0 or lo > ltp or abs(lo - ltp) / ltp > 0.08:
            lo = min(ltp, op)

        typical_price = (hi + lo + ltp) / 3.0
        if inst in self.spot_vwap:
            self.spot_vwap[inst].seed(typical_price, 5000.0)
        if inst in self.spot_ema9:
            self.spot_ema9[inst].seed(ltp)
        if inst in self.spot_ema21:
            self.spot_ema21[inst].seed((ltp + op) / 2.0)
        if inst in self.spot_rsi:
            self.spot_rsi[inst].seed(50.0, ltp)
        if inst in self.spot_adx:
            self.spot_adx[inst].seed(25.0)
        if inst in self.spot_vwap_history:
            self.spot_vwap_history[inst].clear()
            self.spot_vwap_history[inst].extend([typical_price, typical_price, typical_price])

        # Seed intraday high/low from quote
        if inst in self.day_high:
            self.day_high[inst] = hi
            self.day_low[inst] = lo

        logger.info(f"⚡ [VWAP_EMA] Pre-seeded {inst} indicators from spot quote: LTP={ltp}, VWAP={typical_price:.1f}, EMA9={ltp:.1f}, EMA21={(ltp+op)/2.0:.1f}, DayHi={hi:.1f}, DayLo={lo:.1f}")

    def seed_from_candles(self, inst: str, candles: List[Dict[str, Any]]):
        """Feeds historical 3-minute candles to fully warm up VWAP, EMAs, RSI, and ADX."""
        if not candles:
            return
        logger.info(f"🔄 [VWAP_EMA] Warming up {inst} indicators with {len(candles)} historical candles...")
        for c in candles:
            hi = float(c.get("high", 0.0))
            lo = float(c.get("low", 0.0))
            cl = float(c.get("close", 0.0))
            vol = float(c.get("volume", 1000.0))
            if cl <= 0:
                continue

            typical = (hi + lo + cl) / 3.0
            if inst in self.spot_vwap:
                self.spot_vwap[inst].update(typical, vol)
            if inst in self.spot_ema9:
                self.spot_ema9[inst].update(cl)
            if inst in self.spot_ema21:
                self.spot_ema21[inst].update(cl)
            if inst in self.spot_rsi:
                self.spot_rsi[inst].update(cl)
            if inst in self.spot_adx:
                self.spot_adx[inst].update(hi, lo, cl)

            # Track day high / day low
            if inst in self.day_high:
                self.day_high[inst] = max(self.day_high[inst], hi)
                self.day_low[inst] = min(self.day_low[inst], lo)

            # Track rolling VWAP history for slope
            if inst in self.spot_vwap and inst in self.spot_vwap_history:
                cur_vwap = self.spot_vwap[inst].value
                self.spot_vwap_history[inst].append(cur_vwap)

        logger.info(f"✅ [VWAP_EMA] {inst} warmed up! VWAP={self.spot_vwap[inst].value:.1f}, EMA9={self.spot_ema9[inst].value:.1f}, EMA21={self.spot_ema21[inst].value:.1f}, RSI={self.spot_rsi[inst].value:.1f}, ADX={self.spot_adx[inst].value:.1f}")


    def _check_exhaustion_filter(self, inst: str, direction: str, spot: float, adx: float = 0.0, vwap_slope: float = 0.0) -> bool:
        """
        Returns True if the signal should be SUPPRESSED due to trend exhaustion.
        
        DYNAMIC TREND-DAY OVERRIDE:
        If ADX >= 28.0 and VWAP Slope confirms strong directional momentum:
        - For PE: vwap_slope <= -0.35 pt/bar
        - For CE: vwap_slope >= +0.35 pt/bar
        The market is in an impulsive institutional trend session, not a range.
        Exhaustion ceiling is relaxed from 2.0× ATR up to 5.0× ATR because
        multi-leg trend days and market sell-offs routinely run 3× to 5× ATR.
        """
        atr = self.spot_atr_value.get(inst, 0.0)
        # Apply ATR floor to prevent undersized ATR from the seeder
        atr_floor = self.min_atr_floor.get(inst, 30.0) if isinstance(self.min_atr_floor, dict) else 30.0
        effective_atr = max(atr, atr_floor)

        multiplier = self.exhaustion_atr_multiplier
        is_strong_trend = False
        if adx >= 28.0:
            if (direction == "PE" and vwap_slope <= -0.35) or (direction == "CE" and vwap_slope >= 0.35):
                multiplier = max(multiplier, 5.0)
                is_strong_trend = True

        threshold = multiplier * effective_atr

        if direction == "PE":
            day_hi = self.day_high.get(inst, spot)
            move_from_high = day_hi - spot
            if move_from_high > threshold:
                logger.info(
                    f"🚧 [EXHAUSTION FILTER] {inst} PE suppressed: "
                    f"Spot fallen {move_from_high:.1f} pts from day high {day_hi:.1f} "
                    f"(threshold: {threshold:.1f} = {multiplier}× ATR {effective_atr:.1f}, StrongTrend={is_strong_trend})"
                )
                return True
        elif direction == "CE":
            day_lo = self.day_low.get(inst, spot)
            move_from_low = spot - day_lo
            if move_from_low > threshold:
                logger.info(
                    f"🚧 [EXHAUSTION FILTER] {inst} CE suppressed: "
                    f"Spot risen {move_from_low:.1f} pts from day low {day_lo:.1f} "
                    f"(threshold: {threshold:.1f} = {multiplier}× ATR {effective_atr:.1f}, StrongTrend={is_strong_trend})"
                )
                return True
        return False

    def _check_vwap_distance_filter(self, direction: str, spot: float, vwap: float, tested_ema9: bool = False, adx: float = 0.0) -> bool:
        """
        Returns True if the signal should be SUPPRESSED because spot is too far
        from VWAP in the trade's direction (chasing rather than pulling back).
        
        DYNAMIC TREND-DAY OVERRIDE:
        If the candle tested EMA9 and ADX >= 25.0, this is a legitimate fast-EMA
        trend pullback! Price in strong trends rarely pulls all the way back to VWAP.
        In this scenario, relax max distance from 0.30% to 0.75%.
        """
        if vwap <= 0:
            return False
        vwap_dist_pct = abs(spot - vwap) / vwap * 100.0

        max_dist = self.max_vwap_distance_pct
        if tested_ema9 and adx >= 25.0:
            max_dist = max(max_dist, 0.75)  # Allow shallow EMA9 pullbacks in strong trends
        elif adx >= 28.0:
            max_dist = max(max_dist, 0.60)

        if direction == "PE" and spot < vwap and vwap_dist_pct > max_dist:
            logger.info(
                f"🚧 [VWAP DISTANCE FILTER] PE suppressed: Spot {spot:.1f} is {vwap_dist_pct:.2f}% "
                f"below VWAP {vwap:.1f} (max: {max_dist}%). "
                f"This is a chase, not a pullback."
            )
            return True
        elif direction == "CE" and spot > vwap and vwap_dist_pct > max_dist:
            logger.info(
                f"🚧 [VWAP DISTANCE FILTER] CE suppressed: Spot {spot:.1f} is {vwap_dist_pct:.2f}% "
                f"above VWAP {vwap:.1f} (max: {max_dist}%). "
                f"This is a chase, not a pullback."
            )
            return True
        return False

    async def on_tick(self, tick: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        raw_ts = tick.get("exchange_timestamp") or datetime.now(timezone.utc).timestamp()
        ts = float(raw_ts) if raw_ts > 1e11 else float(raw_ts)
        if ts > 1e11:
            ts /= 1000.0

        now_dt = self.parse_ist_time(ts)
        if not self.is_time_gated(now_dt, self.start_time, self.end_time):
            return None

        token = str(tick.get("token", ""))
        ltp = float(tick.get("ltp", 0.0))
        if ltp <= 0:
            return None

        # Spot candle & indicator updates
        if meta and meta.get("is_spot"):
            inst = meta.get("name", "NIFTY")
            # Spot sanity filter: reject anomalies or wrong token data
            if inst == "NIFTY" and (ltp < 15000.0 or ltp > 40000.0):
                return None
            if inst == "SENSEX" and (ltp < 50000.0 or ltp > 120000.0):
                return None

            # Update spot VWAP with uniform tick weight 1.0 (indexes have no direct orderbook volume)
            self.spot_vwap[inst].update(ltp, 1.0)

            # Track intraday high/low for exhaustion filter
            if ltp > self.day_high.get(inst, -1.0):
                self.day_high[inst] = ltp
            if ltp < self.day_low.get(inst, 1e9):
                self.day_low[inst] = ltp

            closed = self.spot_3m_aggregators[inst].on_tick(ts, ltp, volume=1.0)
            if closed:
                o, h, l, c = closed["open"], closed["high"], closed["low"], closed["close"]
                ema9_val = self.spot_ema9[inst].update(c)
                ema21_val = self.spot_ema21[inst].update(c)
                rsi_val = self.spot_rsi[inst].update(c)
                vwap_val = self.spot_vwap[inst].value

                # Self-healing sanity anchor: an index VWAP cannot mathematically deviate > 2% from candle close
                if abs(vwap_val - c) / c > 0.02:
                    logger.warning(f"⚠️ [VWAP SANITY] Re-anchoring distorted {inst} VWAP ({vwap_val:.1f}) to candle close ({c:.1f})")
                    self.spot_vwap[inst].seed(c, 5000.0)
                    vwap_val = c

                adx_val = self.spot_adx[inst].update(h, l, c)

                # Capture ATR-like measure from ADX's internal TR for exhaustion filter
                candle_range = h - l
                prev_atr = self.spot_atr_value.get(inst, candle_range)
                self.spot_atr_value[inst] = prev_atr * 0.9 + candle_range * 0.1  # EMA-smoothed

                self.spot_vwap_history[inst].append(vwap_val)
                vh = list(self.spot_vwap_history[inst])
                vwap_slope = ((vh[-1] - vh[0]) / (len(vh) - 1)) if len(vh) >= 3 else 0.0

                self.spot_3m_vols[inst].append(closed["volume"])
                vols = list(self.spot_3m_vols[inst])
                avg_vol = (sum(vols[:-1]) / len(vols[:-1])) if len(vols) > 1 else closed["volume"]
                vol_ratio = (closed["volume"] / avg_vol) if avg_vol > 0 else 1.25

                # Evaluate Volume Dynamics Detectors
                v_dryup = self.dryup_detectors[inst].update(closed)
                v_contact_vwap_ce = self.contact_detectors[inst].check_contact(closed, vwap_val, "VWAP", direction="CE")
                v_contact_ema9_ce = self.contact_detectors[inst].check_contact(closed, ema9_val, "EMA9", direction="CE")
                v_contact_vwap_pe = self.contact_detectors[inst].check_contact(closed, vwap_val, "VWAP", direction="PE")
                v_contact_ema9_pe = self.contact_detectors[inst].check_contact(closed, ema9_val, "EMA9", direction="PE")

                # 1. Check if we were awaiting confirmation from prior rejection candle
                awaiting = self.awaiting_confirmation[inst]
                if awaiting:
                    aw_adx = awaiting.get("adx", adx_val)
                    aw_slope = awaiting.get("vwap_slope", vwap_slope)
                    aw_tested_ema9 = awaiting.get("tested_ema9", False)

                    if awaiting["direction"] == "CE":
                        # Confirmation candle must close above EMA9 and show bullishness
                        if c > ema9_val and c >= o:
                            # Apply anti-chop filters before confirming (with dynamic trend-day awareness)
                            if self.is_in_stop_cooldown(inst, "CE", ts):
                                self.awaiting_confirmation[inst] = None
                                return None
                            if self._check_exhaustion_filter(inst, "CE", c, adx=aw_adx, vwap_slope=aw_slope):
                                self.awaiting_confirmation[inst] = None
                                return None
                            if self._check_vwap_distance_filter("CE", c, vwap_val, tested_ema9=aw_tested_ema9, adx=aw_adx):
                                self.awaiting_confirmation[inst] = None
                                return None

                            # Rejection confirmed!
                            custom_sl = min(awaiting["rejection_candle"]["low"], l, ema9_val) - 5.0
                            conditions = {
                                "trend_alignment": (ema9_val > ema21_val and c > vwap_val),
                                "volume_confirmation": (awaiting["vol_ratio"] >= self.min_vol_ratio or awaiting.get("volume_contact", {}).get("is_contact", False)),
                                "momentum_strength": min(1.0, max(0.1, (c - o) / max(5.0, (h - l)))),
                                "time_quality": self.is_time_gated(now_dt, self.start_time, self.end_time),
                                "context_filter": (awaiting.get("bottom_wick", 0.0) >= 0.45 and awaiting.get("adx", 20.0) >= 25.0),
                                "volume_contact_bonus": awaiting.get("volume_contact", {}).get("boost_score", 0),
                                "volume_dryup_bonus": awaiting.get("volume_dryup", {}).get("boost_score", 0)
                            }
                            conf = self.compute_confidence(conditions)
                            if conf >= self.confidence_threshold:
                                self.pending_signal[inst] = {
                                    "direction": "CE",
                                    "spot_close": c,
                                    "custom_sl": custom_sl,
                                    "confidence": conf,
                                    "rsi": rsi_val,
                                    "volume_contact": awaiting.get("volume_contact"),
                                    "volume_dryup": awaiting.get("volume_dryup"),
                                    "time": ts
                                }
                        self.awaiting_confirmation[inst] = None

                    elif awaiting["direction"] == "PE":
                        # Confirmation candle must close below EMA9 and show bearishness
                        if c < ema9_val and c <= o:
                            # Apply anti-chop filters before confirming (with dynamic trend-day awareness)
                            if self.is_in_stop_cooldown(inst, "PE", ts):
                                self.awaiting_confirmation[inst] = None
                                return None
                            if self._check_exhaustion_filter(inst, "PE", c, adx=aw_adx, vwap_slope=aw_slope):
                                self.awaiting_confirmation[inst] = None
                                return None
                            if self._check_vwap_distance_filter("PE", c, vwap_val, tested_ema9=aw_tested_ema9, adx=aw_adx):
                                self.awaiting_confirmation[inst] = None
                                return None

                            custom_sl = max(awaiting["rejection_candle"]["high"], h, ema9_val) + 5.0
                            conditions = {
                                "trend_alignment": (ema9_val < ema21_val and c < vwap_val),
                                "volume_confirmation": (awaiting["vol_ratio"] >= self.min_vol_ratio or awaiting.get("volume_contact", {}).get("is_contact", False)),
                                "momentum_strength": min(1.0, max(0.1, (o - c) / max(5.0, (h - l)))),
                                "time_quality": self.is_time_gated(now_dt, self.start_time, self.end_time),
                                "context_filter": (awaiting.get("top_wick", 0.0) >= 0.45 and awaiting.get("adx", 20.0) >= 25.0),
                                "volume_contact_bonus": awaiting.get("volume_contact", {}).get("boost_score", 0),
                                "volume_dryup_bonus": awaiting.get("volume_dryup", {}).get("boost_score", 0)
                            }
                            conf = self.compute_confidence(conditions)
                            if conf >= self.confidence_threshold:
                                self.pending_signal[inst] = {
                                    "direction": "PE",
                                    "spot_close": c,
                                    "custom_sl": custom_sl,
                                    "confidence": conf,
                                    "rsi": rsi_val,
                                    "volume_contact": awaiting.get("volume_contact"),
                                    "volume_dryup": awaiting.get("volume_dryup"),
                                    "time": ts
                                }
                        self.awaiting_confirmation[inst] = None

                # 2. Check for fresh candidate rejection setup (Candle 1)
                contact_ce = v_contact_vwap_ce if v_contact_vwap_ce["is_contact"] else v_contact_ema9_ce
                contact_pe = v_contact_vwap_pe if v_contact_vwap_pe["is_contact"] else v_contact_ema9_pe
                has_vol_boost_ce = (vol_ratio >= self.min_vol_ratio) or contact_ce["is_contact"] or v_dryup["is_ignition"]
                has_vol_boost_pe = (vol_ratio >= self.min_vol_ratio) or contact_pe["is_contact"] or v_dryup["is_ignition"]

                # Check for dry-up squeeze pre-alert: ONLY fire once when squeeze reaches threshold and price is near VWAP
                if (
                    v_dryup.get("is_dry_up")
                    and v_dryup.get("dry_bars") == self.dryup_detectors[inst].min_dry_bars
                    and abs(c - vwap_val) / vwap_val <= 0.003
                ):
                    self.emit_radar_alert(
                        alert_type="VOLUME_DRYUP_SQUEEZE",
                        instrument=inst,
                        direction="CE" if c >= vwap_val else "PE",
                        title=f"🪫 {inst} Volume Dry-Up Squeeze",
                        message=f"{inst} volume dried up for {v_dryup['dry_bars']} consecutive bars near VWAP ({vwap_val:.1f}). Range compressing — watch for ignition!",
                        meta_details=v_dryup,
                        now_ts=ts
                    )


                if not self.awaiting_confirmation[inst] and adx_val >= self.min_adx:
                    if ema9_val and ema21_val and (ema9_val > ema21_val) and (c > vwap_val) and (vwap_slope >= self.min_vwap_slope) and has_vol_boost_ce:
                        tested_ema9 = (l <= ema9_val * 1.0005 and c > ema9_val)
                        tested_vwap = (abs(l - vwap_val) <= (self.vwap_tolerance * c))
                        bottom_wick = calculate_bottom_wick_ratio(o, h, l, c)

                        if (tested_ema9 or tested_vwap) and (bottom_wick >= self.min_bottom_wick):
                            if self.rsi_ce_min <= rsi_val <= self.rsi_ce_max:
                                if contact_ce["is_contact"]:
                                    logger.info(f"🎯 [VWAP_EMA CONTACT] {inst} CE tested {contact_ce['level_name']} ({contact_ce['level_price']:.1f}) with {contact_ce['vol_ratio']:.2f}x volume! Wick: {contact_ce['rejection_wick']:.2f}")
                                    self.emit_radar_alert(
                                        alert_type="VOLUME_CONTACT",
                                        instrument=inst,
                                        direction="CE",
                                        title=f"⚡ {inst} Institutional Volume Defense",
                                        message=f"{inst} defended {contact_ce['level_name']} ({contact_ce['level_price']:.1f}) with {contact_ce['vol_ratio']:.1f}x volume! Bullish bounce forming.",
                                        meta_details=contact_ce,
                                        now_ts=ts
                                    )
                                elif v_dryup["is_ignition"]:
                                    logger.info(f"🔥 [VWAP_EMA DRY-UP IGNITION] {inst} CE ignition after {v_dryup['dry_bars']} dry bars! Ratio: {v_dryup['ignition_ratio']:.2f}x")
                                    self.emit_radar_alert(
                                        alert_type="DRYUP_IGNITION",
                                        instrument=inst,
                                        direction="CE",
                                        title=f"🔥 {inst} Volume Dry-Up Ignition",
                                        message=f"{inst} broke out after {v_dryup['dry_bars']} dry bars with {v_dryup['ignition_ratio']:.1f}x volume! Bullish expansion underway.",
                                        meta_details=v_dryup,
                                        now_ts=ts
                                    )
                                else:
                                    self.emit_radar_alert(
                                        alert_type="PULLBACK_REJECTION",
                                        instrument=inst,
                                        direction="CE",
                                        title=f"👀 {inst} VWAP/EMA Pullback Detected",
                                        message=f"{inst} printed lower-wick bounce candle at {c:.1f}. Awaiting confirmation candle for CE entry.",
                                        now_ts=ts
                                    )

                                # If this candle is an explosive Volume Ignition or Institutional Defense with green close, trigger immediately!
                                is_immediate_ignition = (contact_ce["is_contact"] and c > o and c > ema9_val) or (v_dryup["is_ignition"] and c > o and c > ema9_val)
                                if is_immediate_ignition:
                                    if not self.is_in_stop_cooldown(inst, "CE", ts) and not self._check_exhaustion_filter(inst, "CE", c, adx=adx_val, vwap_slope=vwap_slope) and not self._check_vwap_distance_filter("CE", c, vwap_val, tested_ema9=tested_ema9, adx=adx_val):
                                        custom_sl = min(l, ema9_val) - 5.0
                                        conditions = {
                                            "trend_alignment": (ema9_val > ema21_val and c > vwap_val),
                                            "volume_confirmation": True,
                                            "momentum_strength": min(1.0, max(0.1, (c - o) / max(5.0, (h - l)))),
                                            "time_quality": self.is_time_gated(now_dt, self.start_time, self.end_time),
                                            "context_filter": (bottom_wick >= 0.35 and adx_val >= 25.0),
                                            "volume_contact_bonus": contact_ce.get("boost_score", 0),
                                            "volume_dryup_bonus": v_dryup.get("boost_score", 0)
                                        }
                                        conf = self.compute_confidence(conditions)
                                        if conf >= self.confidence_threshold:
                                            logger.info(f"🚀 [VWAP_EMA IGNITION TRIGGER] Immediate confirmed {inst} CE entry on {('Volume Contact' if contact_ce['is_contact'] else 'Dry-Up Ignition')}! Conf={conf}%")
                                            self.pending_signal[inst] = {
                                                "direction": "CE",
                                                "spot_close": c,
                                                "custom_sl": custom_sl,
                                                "confidence": conf,
                                                "rsi": rsi_val,
                                                "volume_contact": contact_ce,
                                                "volume_dryup": v_dryup,
                                                "time": ts
                                            }
                                else:
                                    # Passive pullback: await confirmation candle
                                    self.awaiting_confirmation[inst] = {
                                        "direction": "CE",
                                        "rejection_candle": closed,
                                        "ema9": ema9_val,
                                        "vwap": vwap_val,
                                        "rsi": rsi_val,
                                        "adx": adx_val,
                                        "vwap_slope": vwap_slope,
                                        "bottom_wick": bottom_wick,
                                        "vol_ratio": vol_ratio,
                                        "tested_ema9": tested_ema9,
                                        "volume_contact": contact_ce,
                                        "volume_dryup": v_dryup,
                                        "time": ts
                                    }

                    elif ema9_val and ema21_val and (ema9_val < ema21_val) and (c < vwap_val) and (vwap_slope <= -self.min_vwap_slope) and has_vol_boost_pe:
                        tested_ema9 = (h >= ema9_val * 0.9995 and c < ema9_val)
                        tested_vwap = (abs(h - vwap_val) <= (self.vwap_tolerance * c))
                        top_wick = calculate_top_wick_ratio(o, h, l, c)

                        is_bearish_breakdown = (c < o and (o - c) >= 0.35 * max(5.0, (h - l)))
                        if ((tested_ema9 or tested_vwap) and (top_wick >= 0.15)) or is_bearish_breakdown:
                            if self.rsi_pe_min <= rsi_val <= self.rsi_pe_max:
                                if contact_pe["is_contact"]:
                                    logger.info(f"🎯 [VWAP_EMA CONTACT] {inst} PE tested {contact_pe['level_name']} ({contact_pe['level_price']:.1f}) with {contact_pe['vol_ratio']:.2f}x volume! Wick: {contact_pe['rejection_wick']:.2f}")
                                    self.emit_radar_alert(
                                        alert_type="VOLUME_CONTACT",
                                        instrument=inst,
                                        direction="PE",
                                        title=f"⚡ {inst} Institutional Volume Attack",
                                        message=f"{inst} rejected at {contact_pe['level_name']} ({contact_pe['level_price']:.1f}) with {contact_pe['vol_ratio']:.1f}x volume! Bearish breakdown forming.",
                                        meta_details=contact_pe,
                                        now_ts=ts
                                    )
                                elif v_dryup["is_ignition"]:
                                    logger.info(f"🔥 [VWAP_EMA DRY-UP IGNITION] {inst} PE ignition after {v_dryup['dry_bars']} dry bars! Ratio: {v_dryup['ignition_ratio']:.2f}x")
                                    self.emit_radar_alert(
                                        alert_type="DRYUP_IGNITION",
                                        instrument=inst,
                                        direction="PE",
                                        title=f"🔥 {inst} Volume Dry-Up Ignition",
                                        message=f"{inst} broke down after {v_dryup['dry_bars']} dry bars with {v_dryup['ignition_ratio']:.1f}x volume! Bearish expansion underway.",
                                        meta_details=v_dryup,
                                        now_ts=ts
                                    )
                                else:
                                    self.emit_radar_alert(
                                        alert_type="PULLBACK_REJECTION",
                                        instrument=inst,
                                        direction="PE",
                                        title=f"👀 {inst} VWAP/EMA Breakdown Detected",
                                        message=f"{inst} printed upper-wick rejection candle at {c:.1f}. Awaiting confirmation candle for PE entry.",
                                        now_ts=ts
                                    )

                                # If this candle is an explosive Volume Ignition or Institutional Breakdown with red close, trigger immediately!
                                is_immediate_ignition_pe = (contact_pe["is_contact"] and c < o and c < ema9_val) or (v_dryup["is_ignition"] and c < o and c < ema9_val)
                                if is_immediate_ignition_pe:
                                    if not self.is_in_stop_cooldown(inst, "PE", ts) and not self._check_exhaustion_filter(inst, "PE", c, adx=adx_val, vwap_slope=vwap_slope) and not self._check_vwap_distance_filter("PE", c, vwap_val, tested_ema9=tested_ema9, adx=adx_val):
                                        custom_sl = max(h, ema9_val) + 5.0
                                        conditions = {
                                            "trend_alignment": (ema9_val < ema21_val and c < vwap_val),
                                            "volume_confirmation": True,
                                            "momentum_strength": min(1.0, max(0.1, (o - c) / max(5.0, (h - l)))),
                                            "time_quality": self.is_time_gated(now_dt, self.start_time, self.end_time),
                                            "context_filter": (top_wick >= 0.25 and adx_val >= 25.0),
                                            "volume_contact_bonus": contact_pe.get("boost_score", 0),
                                            "volume_dryup_bonus": v_dryup.get("boost_score", 0)
                                        }
                                        conf = self.compute_confidence(conditions)
                                        if conf >= self.confidence_threshold:
                                            logger.info(f"🚀 [VWAP_EMA IGNITION TRIGGER] Immediate confirmed {inst} PE entry on {('Volume Contact' if contact_pe['is_contact'] else 'Dry-Up Ignition')}! Conf={conf}%")
                                            self.pending_signal[inst] = {
                                                "direction": "PE",
                                                "spot_close": c,
                                                "custom_sl": custom_sl,
                                                "confidence": conf,
                                                "rsi": rsi_val,
                                                "volume_contact": contact_pe,
                                                "volume_dryup": v_dryup,
                                                "time": ts
                                            }
                                else:
                                    # Passive pullback: await confirmation candle
                                    self.awaiting_confirmation[inst] = {
                                        "direction": "PE",
                                        "rejection_candle": closed,
                                        "ema9": ema9_val,
                                        "vwap": vwap_val,
                                        "rsi": rsi_val,
                                        "adx": adx_val,
                                        "vwap_slope": vwap_slope,
                                        "top_wick": top_wick,
                                        "vol_ratio": vol_ratio,
                                        "tested_ema9": tested_ema9,
                                        "volume_contact": contact_pe,
                                        "volume_dryup": v_dryup,
                                        "time": ts
                                    }
            return None

        # Option tick processing
        if not meta or not meta.get("option_type"):
            return None

        inst = meta.get("name", "NIFTY")
        opt_type = meta.get("option_type")
        strike = float(meta.get("strike", 0.0))
        lot_size = int(meta.get("lot_size", 50))
        symbol = meta.get("symbol", "")
        is_atm = (meta.get("offset") == 0)

        pending = self.pending_signal.get(inst)
        if pending and is_atm and pending["direction"] == opt_type:
            sig_key = f"VWAP_EMA_{inst}_{opt_type}_{strike}"
            if self.can_trigger(sig_key, ts):
                self.pending_signal[inst] = None
                conf = pending.get("confidence", 75)
                logger.info(f"⚡ [VWAP & EMA] Confirmed Trigger for {symbol} ({opt_type})! Confidence: {conf}%, Spot: {pending['spot_close']:.1f}, SL: {pending.get('custom_sl'):.1f}")

                return self.build_signal_payload(
                    instrument=inst,
                    direction=opt_type,
                    strike=strike,
                    option_type=opt_type,
                    option_token=token,
                    option_symbol=symbol,
                    spot_entry=pending["spot_close"],
                    entry_price=ltp,
                    lot_size=lot_size,
                    confidence=conf,
                    custom_sl_spot=pending.get("custom_sl"),
                    meta_details={
                        "spot_close": pending["spot_close"],
                        "rsi": round(pending.get("rsi", 50.0), 1),
                        "custom_sl": round(pending.get("custom_sl", 0.0), 1),
                        "confidence": conf,
                        "volume_contact": pending.get("volume_contact"),
                        "volume_dryup": pending.get("volume_dryup")
                    }
                )

        return None
