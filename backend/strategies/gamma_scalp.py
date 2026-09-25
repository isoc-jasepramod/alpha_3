from collections import deque
from datetime import datetime, timezone, time
from typing import Dict, Any, Optional
from loguru import logger
from backend.strategies.base_strategy import BaseStrategy
from backend.strategies.indicators import CandleAggregator

class ExpiryDayGammaScalp(BaseStrategy):
    """
    Expiry Day Gamma Scalp (13:15 - 15:30)
    Runs on expiry day.
    Premium Filter: Nifty options ₹25-₹60; Sensex options ₹60-₹150.
    Consolidation window: 12:00 - 13:00 (H_mid, L_mid).
    Breakout window: 13:15 - 15:30.

    TIMING FIX (v2): Spot breakout is now detected on TICK level (not 3-min candle close).
    This fires the signal 30-60 seconds earlier, catching the gamma expansion at its start
    rather than after the premium has already spiked.

    NEW FILTERS:
    1. Premium Staleness: Rejects if option premium has already surged >10% from its
       rolling 3-minute low (you'd be buying the high of the scalp).
    2. WATCH Alert: When spot is within 0.15% of H_mid/L_mid with positive velocity,
       a pre-alert is emitted so the trader can prepare.
    3. Tighter consolidation: max_mid_range_pct lowered to 0.3% (from 0.5%).

    CE Trigger: Spot breaks > H_mid with tick-level velocity.
    PE Trigger: Spot breaks < L_mid with tick-level velocity.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(name="GAMMA_SCALP")
        cfg = config or {}
        self.start_time = cfg.get("start_time", "13:15:00")
        self.end_time = cfg.get("end_time", "15:30:00")
        self.mid_start = cfg.get("consolidation_start", "12:00:00")
        self.mid_end = cfg.get("consolidation_end", "13:00:00")
        self.min_roc_pct = cfg.get("min_roc_3m_pct", 0.06)
        self.max_mid_range_pct = cfg.get("max_mid_range_pct", 0.003)  # Tightened from 0.005
        self.vol_multiplier = cfg.get("min_vol_multiplier", 2.5)
        self.enforce_expiry_day = cfg.get("enforce_expiry_day", False)

        # Premium staleness check: reject if premium already surged > X% from its 3-min low
        self.premium_staleness_pct = cfg.get("premium_staleness_pct", 10.0)
        # WATCH alert proximity: fire when spot is within X% of H_mid/L_mid
        self.watch_proximity_pct = cfg.get("watch_proximity_pct", 0.15)

        # Midday consolidation range
        self.mid_ranges: Dict[str, Dict[str, float]] = {
            "NIFTY": {"high": -1.0, "low": 1e9, "finalized": False},
            "SENSEX": {"high": -1.0, "low": 1e9, "finalized": False}
        }

        # TICK-LEVEL spot tracking (replaces 3-min candle close for breakout detection)
        self.spot_tick_history: Dict[str, deque] = {
            "NIFTY": deque(maxlen=60),  # ~60 ticks for velocity estimation
            "SENSEX": deque(maxlen=60)
        }

        # Keep 3-min candle close for supplementary RoC (displayed in signal details)
        self.spot_3m_history: Dict[str, deque] = {
            "NIFTY": deque(maxlen=10),
            "SENSEX": deque(maxlen=10)
        }
        self.spot_3m_agg: Dict[str, CandleAggregator] = {
            "NIFTY": CandleAggregator(timeframe_seconds=180),
            "SENSEX": CandleAggregator(timeframe_seconds=180)
        }

        # 1-min option volume aggregators
        self.opt_1m_agg: Dict[str, CandleAggregator] = {}
        self.opt_1m_vols: Dict[str, deque] = {}

        # Rolling option premium low tracker (3-minute window) for staleness check
        self.opt_premium_low: Dict[str, deque] = {}  # token -> deque of (ts, ltp)

        # WATCH alert tracking (avoid spamming)
        self._watch_emitted: Dict[str, float] = {}  # "NIFTY_CE" -> last_watch_ts
        self._watch_cooldown_sec = 120  # 2 min between WATCH alerts

    def _in_consolidation_window(self, dt: datetime) -> bool:
        t = dt.time()
        sh, sm, ss = map(int, self.mid_start.split(":"))
        eh, em, es = map(int, self.mid_end.split(":"))
        return time(sh, sm, ss) <= t <= time(eh, em, es)

    def _in_gamma_window(self, dt: datetime) -> bool:
        t = dt.time()
        sh, sm, ss = map(int, self.start_time.split(":"))
        eh, em, es = map(int, self.end_time.split(":"))
        return time(sh, sm, ss) <= t <= time(eh, em, es)

    def seed_from_spot(self, inst: str, spot_info: Dict[str, Any]):
        ltp = float(spot_info.get("ltp", 0.0))
        hi = float(spot_info.get("high", ltp))
        lo = float(spot_info.get("low", ltp))
        if ltp <= 0:
            return

        now_dt = self.parse_ist_time(datetime.now(timezone.utc).timestamp())
        t = now_dt.time()
        if t >= time(12, 0, 0):
            rng = self.mid_ranges.setdefault(inst, {"high": -1.0, "low": 1e9, "finalized": False})
            if rng.get("high", -1.0) <= 0 or rng.get("low", 1e9) >= 1e8:
                rng["high"] = round(ltp * 1.002, 2)
                rng["low"] = round(ltp * 0.998, 2)
                if t >= time(13, 0, 0):
                    rng["finalized"] = True
                logger.info(f"⚡ [GAMMA_SCALP] Pre-seeded {inst} midday range: High={rng['high']}, Low={rng['low']}, Finalized={rng.get('finalized')}")

        if inst in self.spot_3m_history:
            cur_ts = now_dt.timestamp()
            self.spot_3m_history[inst].clear()
            self.spot_3m_history[inst].append((cur_ts - 180, ltp))
            self.spot_3m_history[inst].append((cur_ts, ltp))

        if inst in self.spot_tick_history:
            cur_ts = now_dt.timestamp()
            self.spot_tick_history[inst].clear()
            self.spot_tick_history[inst].append((cur_ts, ltp))

    def _compute_tick_velocity(self, inst: str, lookback_sec: float = 30.0) -> float:
        """
        Compute spot velocity (RoC %) over the last `lookback_sec` seconds using tick data.
        Returns the rate of change as a percentage.
        """
        ticks = list(self.spot_tick_history.get(inst, []))
        if len(ticks) < 2:
            return 0.0

        now_ts = ticks[-1][0]
        now_price = ticks[-1][1]

        # Find the oldest tick within the lookback window
        for ts_val, price_val in ticks:
            if now_ts - ts_val <= lookback_sec:
                if price_val > 0:
                    return ((now_price - price_val) / price_val) * 100.0
                break
        return 0.0

    def _get_premium_3m_low(self, token: str, now_ts: float) -> float:
        """Get the lowest premium price for this token in the last 3 minutes."""
        if token not in self.opt_premium_low:
            return 0.0
        entries = self.opt_premium_low[token]
        min_price = 1e9
        for ts_val, price_val in entries:
            if now_ts - ts_val <= 180.0:  # 3-minute window
                min_price = min(min_price, price_val)
        return min_price if min_price < 1e9 else 0.0

    async def on_tick(self, tick: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        raw_ts = tick.get("exchange_timestamp") or datetime.now(timezone.utc).timestamp()
        ts = float(raw_ts) if raw_ts > 1e11 else float(raw_ts)
        if ts > 1e11:
            ts /= 1000.0

        now_dt = self.parse_ist_time(ts)
        token = str(tick.get("token", ""))
        ltp = float(tick.get("ltp", 0.0))
        vol = float(tick.get("volume", 1.0))

        # Spot index updates
        if meta and meta.get("is_spot"):
            inst = meta.get("name", "NIFTY")

            # 1. Update 12:00 - 13:00 consolidation range
            if self._in_consolidation_window(now_dt):
                rng = self.mid_ranges[inst]
                if ltp > rng["high"]:
                    rng["high"] = ltp
                if ltp < rng["low"]:
                    rng["low"] = ltp
            elif self._in_gamma_window(now_dt):
                self.mid_ranges[inst]["finalized"] = True

            # 2. Track tick-level spot data for velocity computation
            self.spot_tick_history[inst].append((ts, ltp))

            # 3. Track 3-min spot candle for supplementary RoC
            closed = self.spot_3m_agg[inst].on_tick(ts, ltp, volume=vol)
            if closed:
                self.spot_3m_history[inst].append((ts, closed["close"]))

            # 4. WATCH alert: Pre-breakout proximity alert
            if self._in_gamma_window(now_dt):
                rng = self.mid_ranges.get(inst, {})
                if rng.get("finalized"):
                    h_mid = rng.get("high", 0.0)
                    l_mid = rng.get("low", 1e9)
                    mid_range = h_mid - l_mid

                    # Check range quality first
                    if 0 < mid_range <= (self.max_mid_range_pct * ltp):
                        proximity_threshold = ltp * self.watch_proximity_pct / 100.0
                        tick_vel = self._compute_tick_velocity(inst, lookback_sec=30.0)

                        # CE WATCH: spot approaching H_mid from below with positive velocity
                        if 0 < (h_mid - ltp) <= proximity_threshold and tick_vel > 0.02:
                            watch_key = f"{inst}_CE"
                            last_watch = self._watch_emitted.get(watch_key, 0)
                            if ts - last_watch > self._watch_cooldown_sec:
                                self._watch_emitted[watch_key] = ts
                                logger.warning(
                                    f"👀 [GAMMA WATCH] {inst} spot {ltp:.1f} approaching H_mid {h_mid:.1f} "
                                    f"(gap: {h_mid - ltp:.1f} pts, velocity: +{tick_vel:.3f}%/30s). "
                                    f"CE breakout imminent — prepare to execute!"
                                )
                                self.emit_radar_alert(
                                    alert_type="GAMMA_WATCH",
                                    instrument=inst,
                                    direction="CE",
                                    title=f"👀 {inst} Gamma Scalp Imminent",
                                    message=f"{inst} spot ({ltp:.1f}) is within {h_mid - ltp:.1f} pts of H_mid ({h_mid:.1f}) with +{tick_vel:.3f}%/30s velocity! Prepare for CE expansion.",
                                    now_ts=ts
                                )

                        # PE WATCH: spot approaching L_mid from above with negative velocity
                        elif 0 < (ltp - l_mid) <= proximity_threshold and tick_vel < -0.02:
                            watch_key = f"{inst}_PE"
                            last_watch = self._watch_emitted.get(watch_key, 0)
                            if ts - last_watch > self._watch_cooldown_sec:
                                self._watch_emitted[watch_key] = ts
                                logger.warning(
                                    f"👀 [GAMMA WATCH] {inst} spot {ltp:.1f} approaching L_mid {l_mid:.1f} "
                                    f"(gap: {ltp - l_mid:.1f} pts, velocity: {tick_vel:.3f}%/30s). "
                                    f"PE breakdown imminent — prepare to execute!"
                                )
                                self.emit_radar_alert(
                                    alert_type="GAMMA_WATCH",
                                    instrument=inst,
                                    direction="PE",
                                    title=f"👀 {inst} Gamma Scalp Imminent",
                                    message=f"{inst} spot ({ltp:.1f}) is within {ltp - l_mid:.1f} pts of L_mid ({l_mid:.1f}) with {tick_vel:.3f}%/30s velocity! Prepare for PE expansion.",
                                    now_ts=ts
                                )


            return None

        # Option tick processing
        if not self._in_gamma_window(now_dt):
            return None

        if not meta or not meta.get("option_type"):
            return None

        inst = meta.get("name", "NIFTY")

        # Expiry Day verification: NIFTY (Tuesday=1) & SENSEX (Friday=4)
        if self.enforce_expiry_day:
            is_weekly_expiry = (inst == "NIFTY" and now_dt.weekday() == 1) or (inst == "SENSEX" and now_dt.weekday() == 4)
            exp_str = meta.get("expiry", "")
            is_contract_expiry_today = False
            for fmt in ("%d%b%Y", "%d-%b-%Y", "%d%B%Y"):
                try:
                    exp_date = datetime.strptime(exp_str, fmt).date()
                    if exp_date == now_dt.date():
                        is_contract_expiry_today = True
                    break
                except ValueError:
                    continue
            if not (is_weekly_expiry or is_contract_expiry_today):
                return None

        opt_type = meta.get("option_type")
        strike = float(meta.get("strike", 0.0))
        lot_size = int(meta.get("lot_size", 50))
        symbol = meta.get("symbol", "")

        # Premium Filter: NIFTY 25-60, SENSEX 60-150
        min_p = 25.0 if inst == "NIFTY" else 60.0
        max_p = 60.0 if inst == "NIFTY" else 150.0
        if not (min_p <= ltp <= max_p):
            return None

        # Track rolling premium low for staleness check
        if token not in self.opt_premium_low:
            self.opt_premium_low[token] = deque(maxlen=200)
        self.opt_premium_low[token].append((ts, ltp))

        # Track 1-min option volume
        if token not in self.opt_1m_agg:
            self.opt_1m_agg[token] = CandleAggregator(timeframe_seconds=60)
            self.opt_1m_vols[token] = deque(maxlen=25)

        closed_opt = self.opt_1m_agg[token].on_tick(ts, ltp, volume=vol)
        if closed_opt:
            self.opt_1m_vols[token].append(closed_opt.get("volume", 0.0))

        # Check Spot Breakout using TICK-LEVEL data (not 3-min candle close!)
        rng = self.mid_ranges.get(inst, {})
        if not rng.get("finalized"):
            return None

        h_mid = rng.get("high", 0.0)
        l_mid = rng.get("low", 1e9)

        # Use tick-level spot data for instant breakout detection
        spot_ticks = list(self.spot_tick_history.get(inst, []))
        if len(spot_ticks) < 2:
            return None

        curr_spot = spot_ticks[-1][1]

        # Compute tick-level velocity (30-second lookback)
        tick_velocity = self._compute_tick_velocity(inst, lookback_sec=30.0)

        # Also compute 3-min RoC for signal details
        spot_hist_3m = list(self.spot_3m_history.get(inst, []))
        roc_3m = 0.0
        if len(spot_hist_3m) >= 2:
            prev_spot = spot_hist_3m[-2][1]
            roc_3m = ((curr_spot - prev_spot) / prev_spot) * 100.0

        # Consolidation range quality check: range < max_mid_range_pct of spot
        mid_range = h_mid - l_mid
        if mid_range > (self.max_mid_range_pct * curr_spot) or mid_range <= 0:
            return None

        # CE Breakout: tick-level detection with velocity
        is_ce_breakout = (opt_type == "CE" and curr_spot > h_mid and tick_velocity >= self.min_roc_pct)
        # PE Breakdown: tick-level detection with velocity
        is_pe_breakout = (opt_type == "PE" and curr_spot < l_mid and tick_velocity <= -self.min_roc_pct)

        if not (is_ce_breakout or is_pe_breakout):
            return None

        # PREMIUM STALENESS CHECK: Reject if premium already surged from its 3-min low
        premium_3m_low = self._get_premium_3m_low(token, ts)
        if premium_3m_low > 0:
            premium_surge_pct = ((ltp - premium_3m_low) / premium_3m_low) * 100.0
            if premium_surge_pct >= self.premium_staleness_pct:
                logger.warning(
                    f"🚧 [GAMMA STALE] {symbol} premium already surged +{premium_surge_pct:.1f}% "
                    f"from 3-min low ₹{premium_3m_low:.2f} to ₹{ltp:.2f}. "
                    f"You would be buying the high of the scalp. Suppressed."
                )
                return None

        # Check Volume >= 2.5x SMA20
        vols = list(self.opt_1m_vols.get(token, []))
        vol_confirmed = True
        if len(vols) >= 5:
            sma_vol = sum(vols) / len(vols)
            cur_vol = closed_opt.get("volume", vol) if closed_opt else vol
            if sma_vol > 0 and cur_vol < (self.vol_multiplier * sma_vol):
                return None
            vol_confirmed = (sma_vol > 0 and cur_vol >= (self.vol_multiplier * sma_vol))

        # Consecutive stop cooldown check
        direction = "CE" if is_ce_breakout else "PE"
        if self.is_in_stop_cooldown(inst, direction, ts):
            return None

        # Adaptive SL computation
        if opt_type == "CE":
            risk_spot = max(curr_spot - h_mid, curr_spot * 0.002)
            custom_sl = h_mid - (risk_spot * 0.2)
        else:
            risk_spot = max(l_mid - curr_spot, curr_spot * 0.002)
            custom_sl = l_mid + (risk_spot * 0.2)

        # Confidence Scoring
        effective_roc = max(abs(tick_velocity), abs(roc_3m))
        conditions = {
            "trend_alignment": (effective_roc >= 0.08) if opt_type == "CE" else (effective_roc >= 0.08),
            "volume_confirmation": vol_confirmed,
            "momentum_strength": min(1.0, effective_roc / 0.12),
            "time_quality": self.is_time_gated(now_dt, "13:30:00", "15:00:00"),
            "context_filter": (mid_range <= (0.0025 * curr_spot))  # Tightened from 0.0035
        }
        confidence = self.compute_confidence(conditions)

        if confidence < self.confidence_threshold:
            logger.warning(f"⚠️ [GAMMA SCALP SUPPRESSED] {symbol} score {confidence} < threshold {self.confidence_threshold}")
            return None

        sig_key = f"GAMMA_{inst}_{opt_type}_{strike}"
        if not self.can_trigger(sig_key, ts):
            return None

        logger.info(
            f"⚡ [GAMMA SCALP] Triggered for {symbol} ({opt_type})! "
            f"Confidence: {confidence}%, Premium: {ltp:.1f}, "
            f"Tick Velocity: {tick_velocity:.3f}%/30s, 3m-RoC: {roc_3m:.2f}%, "
            f"Spot: {curr_spot:.1f}, 3m-Low Premium: ₹{premium_3m_low:.1f}"
        )

        return self.build_signal_payload(
            instrument=inst,
            direction=opt_type,
            strike=strike,
            option_type=opt_type,
            option_token=token,
            option_symbol=symbol,
            spot_entry=curr_spot,
            entry_price=ltp,
            lot_size=lot_size,
            confidence=confidence,
            custom_sl_spot=custom_sl,
            meta_details={
                "roc_3m": round(roc_3m, 2),
                "tick_velocity_30s": round(tick_velocity, 3),
                "h_mid": h_mid,
                "l_mid": l_mid,
                "mid_range": round(mid_range, 1),
                "entry_premium": ltp,
                "premium_3m_low": round(premium_3m_low, 2),
                "custom_sl": round(custom_sl, 1),
                "confidence": confidence
            }
        )
