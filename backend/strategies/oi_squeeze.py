from collections import deque
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from loguru import logger
from backend.strategies.base_strategy import BaseStrategy
from backend.strategies.indicators import IncrementalEMA, IncrementalADX, CandleAggregator

class OISqueezeSentinel(BaseStrategy):
    """
    The Always-On Sentinel: Live OI Squeeze (09:15 - 15:30)
    Monitors ATM, ATM +/- 1, and ATM +/- 2 strikes over rolling tau = 300s.
    CE Trigger: Delta OI <= -5.0%, Delta Price >= +3.0%, Spot > EMA20, ADX >= 20, Option Vol >= 2x 20-period avg.
    PE Trigger: Delta OI <= -5.0%, Delta Price >= +3.0%, Spot < EMA20, ADX >= 20, Option Vol >= 2x 20-period avg.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None, regime_filter: Optional[Any] = None):
        super().__init__(name="OI_SQUEEZE")
        self.regime_filter = regime_filter
        cfg = config or {}
        self.start_time = cfg.get("start_time", "09:15:00")
        self.end_time = cfg.get("end_time", "15:30:00")
        self.tau = cfg.get("lookback_window_sec", 300) # 300 seconds (5 min)
        self.min_oi_drop_pct = cfg.get("min_oi_drop_pct", -5.0) # -5.0%
        self.min_price_spike_pct = cfg.get("min_price_spike_pct", 3.0) # +3.0%
        self.early_ignition_oi_drop_pct = cfg.get("early_ignition_oi_drop_pct", -2.0) # -2.0%
        self.early_ignition_price_spike_pct_expiry = cfg.get("early_ignition_price_spike_pct_expiry", 8.0) # +8.0% for 0-DTE high gamma
        self.early_ignition_price_spike_pct_standard = cfg.get("early_ignition_price_spike_pct_standard", 12.0) # +12.0% for non-expiry
        self.vol_multiplier = cfg.get("vol_multiplier", 2.0)
        self.min_adx = cfg.get("min_adx", 20.0)
        # Directional confirmation vs EMA20. Require spot to confirm the squeeze direction.
        self.require_spot_confirmation = bool(cfg.get("require_spot_confirmation", True))
        self.spot_confirm_margin_pct = float(cfg.get("spot_confirm_margin_pct", 0.03))  # % of spot

        # Rolling history per option token: deque of (ts, ltp, oi, cumulative_vol)
        self.token_history: Dict[str, deque] = {}
        # 1-min aggregators per option token
        self.aggregators: Dict[str, CandleAggregator] = {}
        # 1-min candle volume history (last 20 candles) per option token
        self.volume_history_1m: Dict[str, deque] = {}

        # ---- v1.1 MEASUREMENT-ONLY state (local to OI Squeeze; does NOT gate any signal) ----
        # Per-token sampled OI-change observations for an OI Z-score. We sample on each OI
        # refresh (OI from the broker updates only ~every 162s), so this captures how unusual
        # the current OI unwind is vs this option's own recent OI behaviour.
        self.oi_change_samples: Dict[str, deque] = {}   # token -> deque of per-refresh ΔOI
        self.last_oi_for_sample: Dict[str, float] = {}  # token -> last OI value seen
        self.last_oi_ts: Dict[str, float] = {}          # token -> ts of last OI refresh

        # Spot tracking
        self.spot_prices: Dict[str, float] = {"NIFTY": 0.0, "SENSEX": 0.0}
        self.spot_ema20: Dict[str, IncrementalEMA] = {
            "NIFTY": IncrementalEMA(period=20),
            "SENSEX": IncrementalEMA(period=20)
        }
        self.spot_adx: Dict[str, IncrementalADX] = {
            "NIFTY": IncrementalADX(period=14),
            "SENSEX": IncrementalADX(period=14)
        }
        self.spot_aggregators: Dict[str, CandleAggregator] = {
            "NIFTY": CandleAggregator(timeframe_seconds=60),
            "SENSEX": CandleAggregator(timeframe_seconds=60)
        }

    def update_spot(self, instrument: str, price: float, ts: float, vol: float = 1.0):
        self.spot_prices[instrument] = price
        if instrument in self.spot_ema20 and self.spot_ema20[instrument].ema is None:
            self.spot_ema20[instrument].seed(price)

        if instrument in self.spot_aggregators:
            closed = self.spot_aggregators[instrument].on_tick(ts, price, vol)
            if closed:
                if instrument in self.spot_ema20:
                    self.spot_ema20[instrument].update(closed["close"])
                if instrument in self.spot_adx:
                    self.spot_adx[instrument].update(closed["high"], closed["low"], closed["close"])
        elif instrument in self.spot_ema20:
            self.spot_ema20[instrument].update(price)

    def seed_from_spot(self, inst: str, spot_info: Dict[str, Any]):
        """Seeds spot price and EMA20 baseline from current spot quote."""
        ltp = float(spot_info.get("ltp", 0.0))
        if ltp > 0:
            self.spot_prices[inst] = ltp
            if inst in self.spot_ema20:
                self.spot_ema20[inst].seed(ltp)
            logger.info(f"⚡ [OI_SQUEEZE] Pre-seeded {inst} spot price ({ltp}) and EMA20.")

    def seed_from_candles(self, inst: str, candles: List[Dict[str, Any]]):
        """Feeds historical candles into EMA20 and ADX14 for instant warm-up."""
        if not candles:
            return
        logger.info(f"🔄 [OI_SQUEEZE] Warming up {inst} EMA20 and ADX with {len(candles)} historical candles...")
        for c in candles:
            cl = float(c.get("close", 0.0))
            hi = float(c.get("high", cl))
            lo = float(c.get("low", cl))
            if cl > 0:
                self.spot_prices[inst] = cl
                if inst in self.spot_ema20:
                    self.spot_ema20[inst].update(cl)
                if inst in self.spot_adx:
                    self.spot_adx[inst].update(hi, lo, cl)
        def _f(x):  # None-safe: EMA20/ADX return None until they reach their period
            return f"{x:.1f}" if isinstance(x, (int, float)) else "warming"
        logger.info(f"✅ [OI_SQUEEZE] {inst} warmed up! EMA20={_f(self.spot_ema20[inst].value)}, ADX={_f(self.spot_adx[inst].value)}")

    def _get_early_ignition_price_threshold(self, inst: str, now_dt: datetime, meta: Optional[Dict[str, Any]] = None) -> float:
        """
        Determines the dynamic Early Ignition threshold.
        NIFTY / SENSEX 0-DTE has massive gamma so +8.0% in rolling tau is a screaming signal.
        Non-expiry contracts require +12.0% to guard against lower-gamma false positives.
        """
        is_expiry_today = False
        if meta and meta.get("expiry"):
            exp_str = str(meta.get("expiry", ""))
            for fmt in ("%d%b%Y", "%d-%b-%Y", "%d%B%Y"):
                try:
                    exp_date = datetime.strptime(exp_str, fmt).date()
                    if exp_date == now_dt.date():
                        is_expiry_today = True
                    break
                except ValueError:
                    continue
        else:
            # Default weekly expiry days: NIFTY (Thursday=3 or Tuesday=1), SENSEX (Friday=4)
            weekday = now_dt.weekday()
            if (inst == "NIFTY" and weekday in (1, 3)) or (inst == "SENSEX" and weekday in (3, 4)):
                is_expiry_today = True

        return self.early_ignition_price_spike_pct_expiry if is_expiry_today else self.early_ignition_price_spike_pct_standard

    def _compute_v11_features(
        self, token: str, inst: str, opt_type: str, hist: "deque", ltp: float, oi: float,
        vol: float, delta_oi_pct: float, delta_price_pct: float, time_span: float,
        tick: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        v1.1 MEASUREMENT-ONLY feature pack. Computed at signal time and attached to the
        signal's meta_details for later backtest analysis. These values DO NOT affect whether
        a signal fires — no caller gates on them. Everything here is local to OI Squeeze.
        """
        import statistics as _stats

        feats: Dict[str, Any] = {}

        # --- multi-horizon price velocity (pts%/sec) + acceleration from the tau window ---
        def price_at_or_before(sec_ago: float):
            target = hist[-1][0] - sec_ago
            chosen = None
            for (t, p, _o, _v) in hist:
                if t <= target:
                    chosen = p
                else:
                    break
            return chosen

        last_ts = hist[-1][0]
        p_now = ltp
        horizons = {}
        for label, sec in (("30s", 30.0), ("60s", 60.0), ("120s", 120.0), ("300s", 300.0)):
            p_old = price_at_or_before(sec)
            if p_old and p_old > 0:
                horizons[label] = round((p_now - p_old) / p_old * 100.0, 2)
        feats["price_move_by_horizon_pct"] = horizons
        # velocity = full-window price %move / elapsed; acceleration = recent vs earlier half
        feats["price_velocity_pct_per_s"] = round(delta_price_pct / max(time_span, 1.0), 4)
        half = horizons.get("60s")
        full = horizons.get("300s")
        if half is not None and full is not None:
            feats["price_acceleration"] = round(half - (full - half), 2)

        # --- OI velocity (%/min over the window) + absolute OI change ---
        old_oi = hist[0][2] if hist else oi
        feats["abs_oi_change"] = round(oi - old_oi, 0)
        feats["oi_velocity_pct_per_min"] = round(delta_oi_pct / max(time_span / 60.0, 0.1), 3)

        # --- OI Z-score: how unusual is this OI unwind vs this option's own refresh steps ---
        samples = list(self.oi_change_samples.get(token, []))
        if len(samples) >= 5:
            mu = _stats.mean(samples)
            sd = _stats.pstdev(samples)
            cur_step = oi - old_oi
            feats["oi_zscore"] = round((cur_step - mu) / sd, 2) if sd > 1e-9 else None
        else:
            feats["oi_zscore"] = None

        # --- Volume Z-score from the 1m candle-volume history (option volume is real) ---
        vols = list(self.volume_history_1m.get(token, []))
        if len(vols) >= 5:
            mu_v = _stats.mean(vols)
            sd_v = _stats.pstdev(vols)
            window_vol = vol - hist[0][3] if hist else 0.0
            feats["volume_zscore"] = round((window_vol - mu_v) / sd_v, 2) if sd_v > 1e-9 else None
            feats["window_volume"] = round(window_vol, 0)
        else:
            feats["volume_zscore"] = None

        # --- spread % from best bid/ask if present on the tick ---
        bid = tick.get("best_bid")
        ask = tick.get("best_ask")
        if bid and ask and bid > 0 and ask > 0 and ask >= bid:
            mid = (ask + bid) / 2.0
            feats["spread_pct"] = round((ask - bid) / mid * 100.0, 3) if mid > 0 else None
        else:
            feats["spread_pct"] = None

        # --- underlying (spot) confirmation: return + velocity over the window ---
        spot = self.spot_prices.get(inst, 0.0)
        ema20 = self.spot_ema20.get(inst).value if self.spot_ema20.get(inst) else None
        feats["spot"] = round(spot, 1) if spot else None
        feats["spot_vs_ema20_pct"] = round((spot - ema20) / ema20 * 100.0, 3) if (ema20 and spot > 0) else None
        # directional agreement: CE wants spot above EMA20, PE below
        if ema20 and spot > 0:
            feats["spot_confirms_direction"] = bool(
                (opt_type == "CE" and spot >= ema20) or (opt_type == "PE" and spot <= ema20)
            )
        else:
            feats["spot_confirms_direction"] = None

        # --- option-vs-underlying efficiency: actual option %move / delta-expected %move ---
        # Expected premium %move ≈ (ATM delta ~0.5 * |spot %move|) * (spot/premium leverage).
        # We approximate leverage with spot/premium; large ratio => move exceeds pure-delta,
        # suggesting gamma/IV/demand/short-covering (a squeeze-strength hint, not a gate).
        spot_move_pct = feats.get("spot_vs_ema20_pct")
        if spot and ltp > 0 and spot_move_pct is not None and abs(spot_move_pct) > 1e-6:
            delta_atm = 0.5
            expected_opt_pct = abs(spot_move_pct) * delta_atm * (spot / ltp)
            if expected_opt_pct > 1e-6:
                feats["option_spot_efficiency"] = round(abs(delta_price_pct) / expected_opt_pct, 2)
            else:
                feats["option_spot_efficiency"] = None
        else:
            feats["option_spot_efficiency"] = None

        # --- squeeze score (0-100), LOGGED ONLY, never gates (per v1.1 plan) ---
        # Weights per the analysis: OI unwind 25, price accel 20, underlying confirm 20,
        # volume abnormality 15, option/spot efficiency 10, flow 10 (flow unavailable -> 0).
        score = 0.0
        # OI unwind strength (25): scale |ΔOI%| from 0 at 2% to full at 10%
        score += max(0.0, min(1.0, (abs(delta_oi_pct) - 2.0) / 8.0)) * 25.0
        # price acceleration (20): scale 0..1 over 0..10 (pct)
        pa = feats.get("price_acceleration")
        if pa is not None:
            score += max(0.0, min(1.0, pa / 10.0)) * 20.0
        # underlying confirmation (20): full if direction confirmed
        if feats.get("spot_confirms_direction"):
            score += 20.0
        # volume abnormality (15): scale z 0..3
        vz = feats.get("volume_zscore")
        if vz is not None:
            score += max(0.0, min(1.0, vz / 3.0)) * 15.0
        # option/spot efficiency (10): >1 means move exceeds pure-delta; scale 1..3
        eff = feats.get("option_spot_efficiency")
        if eff is not None:
            score += max(0.0, min(1.0, (eff - 1.0) / 2.0)) * 10.0
        # flow (10): unavailable (no real CVD) -> 0, by design
        feats["squeeze_score"] = round(score, 1)
        return feats

    async def on_tick(self, tick: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """
        Expects tick with 'token', 'ltp', 'volume', 'open_interest', 'exchange_timestamp'.
        'meta' contains instrument, strike, option_type, lot_size, symbol, etc.
        """
        token = str(tick.get("token", ""))
        ltp = float(tick.get("ltp", 0.0))
        oi = float(tick.get("open_interest", 0.0))
        vol = float(tick.get("volume", 0.0))
        
        # Parse timestamp
        raw_ts = tick.get("exchange_timestamp") or datetime.now(timezone.utc).timestamp()
        ts = float(raw_ts) if raw_ts > 1e11 else float(raw_ts) # handle ms or seconds
        if ts > 1e11:
            ts /= 1000.0

        now_dt = self.parse_ist_time(ts)
        
        # Check time gating (09:15 - 15:30)
        if not self.is_time_gated(now_dt, self.start_time, self.end_time):
            return None

        # If tick is for Spot index
        if meta and meta.get("is_spot"):
            inst = meta.get("name", "NIFTY")
            self.update_spot(inst, ltp, ts, vol)
            return None

        if not meta or not meta.get("option_type"):
            return None

        # Allow ATM and near-ATM strikes (offset in -1, 0, 1) so strike migration does not discard active squeezes
        offset = meta.get("offset")
        if offset is not None and offset not in (-1, 0, 1):
            return None

        inst = meta.get("name", "NIFTY")
        opt_type = meta.get("option_type") # "CE" or "PE"
        strike = float(meta.get("strike", 0.0))
        lot_size = int(meta.get("lot_size", 50))
        symbol = meta.get("symbol", "")

        # 1-min Candle aggregation for volume
        if token not in self.aggregators:
            self.aggregators[token] = CandleAggregator(timeframe_seconds=60)
            self.volume_history_1m[token] = deque(maxlen=25)

        closed_candle = self.aggregators[token].on_tick(ts, ltp, volume=vol)
        if closed_candle:
            self.volume_history_1m[token].append(closed_candle.get("volume", 0.0))

        # Maintain rolling tau window
        if token not in self.token_history:
            self.token_history[token] = deque()

        hist = self.token_history[token]
        hist.append((ts, ltp, oi, vol))

        # Evict ticks older than tau = 300s
        while hist and (ts - hist[0][0]) > self.tau:
            hist.popleft()

        # v1.1 measurement: sample an OI-change observation whenever OI actually refreshes
        # (broker OI updates ~every 162s). Purely for the OI Z-score feature; gates nothing.
        if token not in self.oi_change_samples:
            self.oi_change_samples[token] = deque(maxlen=40)
        prev_oi_s = self.last_oi_for_sample.get(token)
        if prev_oi_s is not None and oi != prev_oi_s:
            self.oi_change_samples[token].append(oi - prev_oi_s)
            self.last_oi_ts[token] = ts
        if prev_oi_s is None or oi != prev_oi_s:
            self.last_oi_for_sample[token] = oi

        # Compare oldest in window vs latest
        old_ts, old_ltp, old_oi, old_vol = hist[0]
        time_span = ts - old_ts

        # Require meaningful baseline OI
        if old_oi <= 100 or old_ltp <= 0.5:
            return None

        delta_oi_pct = ((oi - old_oi) / old_oi) * 100.0
        delta_price_pct = ((ltp - old_ltp) / old_ltp) * 100.0

        # Check squeeze conditions:
        # Standard: dOI <= -5.0%, dPrice >= +3.0%
        # Early Ignition: Dynamic threshold (+8.0% for 0-DTE, +12.0% standard) and dOI <= -2.0% (early unwinding)
        early_price_thresh = self._get_early_ignition_price_threshold(inst, now_dt, meta)
        is_standard_squeeze = (delta_oi_pct <= self.min_oi_drop_pct and delta_price_pct >= self.min_price_spike_pct)
        is_early_ignition = (delta_oi_pct <= self.early_ignition_oi_drop_pct and delta_price_pct >= early_price_thresh)

        if not (is_standard_squeeze or is_early_ignition):
            return None

        # Check positive order flow / CVD if available
        cvd_val = tick.get("cvd")
        buy_qty = tick.get("total_buy_qty", 0.0)
        sell_qty = tick.get("total_sell_qty", 0.0)
        if is_early_ignition and cvd_val is not None:
            if opt_type == "CE" and cvd_val < 0:
                return None
            elif opt_type == "PE" and cvd_val > 0:
                return None
        elif is_early_ignition and buy_qty > 0 and sell_qty > 0:
            if opt_type == "CE" and buy_qty < sell_qty * 0.9:
                return None
            elif opt_type == "PE" and sell_qty < buy_qty * 0.9:
                return None

        # Dynamic Warm-up Guard:
        # Standard squeezes require >= 120s and >= 8 ticks.
        # Early ignition and high-velocity squeezes need only >= 45s and >= 4 ticks.
        is_fast_path = is_early_ignition or (delta_oi_pct <= -8.0 and delta_price_pct >= 5.0)
        min_required_time = 45.0 if is_fast_path else 120.0
        min_ticks = 4 if is_fast_path else 8

        if len(hist) < min_ticks or time_span < min_required_time:
            return None

        if delta_oi_pct <= -4.0 or delta_price_pct >= 2.5:
            logger.debug(f"[OI DEBUG] {token} ({symbol}): dOI={delta_oi_pct:.2f}%, dP={delta_price_pct:.2f}%, span={time_span:.0f}s, old_oi={old_oi}, cur_oi={oi}, old_ltp={old_ltp}, cur_ltp={ltp}")

        # Macro Regime alignment filter
        if self.regime_filter:
            try:
                reg_info = self.regime_filter.get_regime(inst)
                regime = reg_info.get("regime", "NEUTRAL")
                if regime == "TRENDING_BEAR" and opt_type == "CE":
                    logger.info(f"🚫 [OI SQUEEZE REGIME-GATE] {symbol} CE blocked: Macro regime is TRENDING_BEAR ({reg_info.get('score', 0):.1f}).")
                    return None
                elif regime == "TRENDING_BULL" and opt_type == "PE":
                    logger.info(f"🚫 [OI SQUEEZE REGIME-GATE] {symbol} PE blocked: Macro regime is TRENDING_BULL ({reg_info.get('score', 0):.1f}).")
                    return None
            except Exception as e:
                logger.warning(f"Error querying regime filter in OI Squeeze: {e}")

        # 3. Spot vs EMA20 — HARD directional confirmation
        spot = self.spot_prices.get(inst, 0.0)
        ema20 = self.spot_ema20.get(inst).value if self.spot_ema20.get(inst) else None

        if spot <= 0:
            return None
        if ema20 and ema20 > 0:
            if self.require_spot_confirmation:
                # Require spot CLEARLY on the correct side of EMA20 by a positive margin.
                # CE: spot must be above EMA20 by >= margin; PE: below by >= margin.
                margin = ema20 * (self.spot_confirm_margin_pct / 100.0)
                confirms = (
                    (opt_type == "CE" and spot >= ema20 + margin)
                    or (opt_type == "PE" and spot <= ema20 - margin)
                )
                if not confirms:
                    logger.info(
                        f"🚫 [OI SQUEEZE DIR-GATE] {symbol} ({opt_type}) blocked: spot {spot:.1f} not "
                        f"clearly {'above' if opt_type=='CE' else 'below'} EMA20 {ema20:.1f} "
                        f"(margin {self.spot_confirm_margin_pct:.2f}%). Underlying does not confirm direction."
                    )
                    return None
            else:
                # Legacy loose band (kept for reversibility if confirmation is disabled)
                is_bullish_ce = (opt_type == "CE" and spot >= ema20 * 0.9995)
                is_bearish_pe = (opt_type == "PE" and spot <= ema20 * 1.0005)
                if not (is_bullish_ce or is_bearish_pe):
                    return None

        # 4. ADX Filter: Ensure market is trending (ADX >= 20)
        # Fast-Path Exception: Early ignition or extreme squeezes breaking out of tight compression
        # start with low ADX (<20). Do not suppress early ignition signals on compression breakouts.
        adx_val = self.spot_adx.get(inst, IncrementalADX()).value
        if adx_val < self.min_adx and not is_fast_path:
            return None

        # 5. Volume surge check: Option vol in tau >= 2.0x avg 1m volume
        vols = list(self.volume_history_1m.get(token, []))
        vol_confirmed = True
        if len(vols) >= 5 and not is_early_ignition:
            avg_vol = sum(vols) / len(vols)
            window_vol = vol - old_vol
            if avg_vol > 0 and window_vol > 0 and window_vol < (self.vol_multiplier * avg_vol):
                return None
            vol_confirmed = (avg_vol > 0 and window_vol >= (self.vol_multiplier * avg_vol))

        # 6. Confidence Scoring
        spot_confirms_trend = (
            (opt_type == "CE" and spot > (ema20 or spot))
            or (opt_type == "PE" and spot < (ema20 or spot))
        )
        conditions = {
            "trend_alignment": spot_confirms_trend,
            "volume_confirmation": vol_confirmed or is_early_ignition,
            "momentum_strength": 1.0 if is_early_ignition else min(1.0, delta_price_pct / 6.0),
            "time_quality": self.is_time_gated(now_dt, "09:30:00", "15:00:00"),
            "context_filter": (adx_val >= 25.0) or is_early_ignition
        }
        confidence = self.compute_confidence(conditions)

        if confidence < self.confidence_threshold:
            logger.warning(f"⚠️ [OI SQUEEZE SUPPRESSED] {symbol} score {confidence} < threshold {self.confidence_threshold}")
            return None

        # Instrument-wide Cooldown guard (15 mins = 900s)
        sig_key = f"OI_{inst}_{opt_type}"
        self.cooldown_sec = 900
        if not self.can_trigger(sig_key, ts):
            return None

        squeeze_label = "EARLY IGNITION" if is_early_ignition else "STANDARD SQUEEZE"

        # v1.1 MEASUREMENT-ONLY: compute the feature pack AFTER all gates have passed, so it
        # cannot influence whether this signal fires. Attached to meta for backtest analysis.
        v11 = self._compute_v11_features(
            token=token, inst=inst, opt_type=opt_type, hist=hist, ltp=ltp, oi=oi, vol=vol,
            delta_oi_pct=delta_oi_pct, delta_price_pct=delta_price_pct,
            time_span=time_span, tick=tick,
        )
        logger.info(
            f"⚡ [OI SQUEEZE - {squeeze_label}] Triggered for {symbol} ({opt_type})! "
            f"dOI: {delta_oi_pct:.1f}%, dPrice: +{delta_price_pct:.1f}%, Confidence: {confidence}%, "
            f"ADX: {adx_val:.1f} | [v1.1] squeeze_score={v11.get('squeeze_score')}, "
            f"oi_z={v11.get('oi_zscore')}, vol_z={v11.get('volume_zscore')}, "
            f"spot_confirms={v11.get('spot_confirms_direction')}, eff={v11.get('option_spot_efficiency')}, "
            f"spread%={v11.get('spread_pct')}"
        )

        meta_details = {
            "squeeze_type": "EARLY_IGNITION" if is_early_ignition else "STANDARD_SQUEEZE",
            "delta_oi_pct": round(delta_oi_pct, 2),
            "delta_price_pct": round(delta_price_pct, 2),
            "spot": spot,
            "spot_ema20": round(ema20, 2),
            "adx": round(adx_val, 1),
            "lookback_tau_sec": self.tau,
            "confidence": confidence,
            "v11": v11,  # measurement-only feature pack (does not affect signal firing)
        }

        return self.build_signal_payload(
            instrument=inst,
            direction=opt_type,
            strike=strike,
            option_type=opt_type,
            option_token=token,
            option_symbol=symbol,
            spot_entry=spot,
            entry_price=ltp,
            lot_size=lot_size,
            confidence=confidence,
            meta_details=meta_details,
        )
