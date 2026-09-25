from collections import deque
from datetime import datetime, timezone, time
from typing import Dict, Any, Optional
from loguru import logger
from backend.strategies.base_strategy import BaseStrategy
from backend.strategies.indicators import (
    CandleAggregator,
    VolumeContactDetector,
    VolumeDryUpDetector
)


class VolumeBackedORB(BaseStrategy):
    """
    Volume-Backed ORB (09:15 - 10:30)
    Range defined between 09:15 and 09:30 (H_ORB, L_ORB).
    Buffer margin delta = Spot * 0.0003.
    CE Trigger: 5-min candle closes > (H_ORB + delta) with RVOL >= 1.50.
    PE Trigger: 5-min candle closes < (L_ORB - delta) with RVOL >= 1.50.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(name="ORB_BREAKOUT")
        cfg = config or {}
        self.start_time = cfg.get("start_time", "09:15:00")
        self.end_time = cfg.get("end_time", "10:30:00")
        self.orb_start_time = cfg.get("orb_window_start", "09:15:00")
        self.orb_end_time = cfg.get("orb_window_end", "09:30:00")
        self.buffer_factor = cfg.get("buffer_margin_factor", 0.0003)
        self.min_rvol = cfg.get("min_rvol", 1.80)
        self.min_range = cfg.get("min_orb_range", 25.0)
        self.max_range = cfg.get("max_orb_range", 200.0)
        self.breakout_strength_pct = cfg.get("breakout_strength_pct", 0.05)
        self.max_gap_pct = cfg.get("max_gap_pct", 0.40) # 0.40% gap threshold for Gap-and-Go
        self.extreme_gap_pct = cfg.get("extreme_gap_pct", 1.50) # 1.50% hard cutoff for hazardous gaps

        # High/Low for ORB range per instrument
        self.orb_ranges: Dict[str, Dict[str, Any]] = {
            "NIFTY": {"high": -1.0, "low": 1e9, "finalized": False},
            "SENSEX": {"high": -1.0, "low": 1e9, "finalized": False}
        }

        # Daily tracking for Gap calculation
        self.day_tracking: Dict[str, Dict[str, Any]] = {
            "NIFTY": {"date": None, "day_open": None, "prev_close": None, "excessive_gap": False, "gap_dir": None, "gap_pct": 0.0},
            "SENSEX": {"date": None, "day_open": None, "prev_close": None, "excessive_gap": False, "gap_dir": None, "gap_pct": 0.0}
        }

        # 5-min candle aggregators on Spot
        self.spot_5m_aggregators: Dict[str, CandleAggregator] = {
            "NIFTY": CandleAggregator(timeframe_seconds=300),
            "SENSEX": CandleAggregator(timeframe_seconds=300)
        }

        # Spot volume dynamics detectors (Volume Contact & Dry-Up Ignition)
        self.spot_contact_detectors: Dict[str, VolumeContactDetector] = {
            "NIFTY": VolumeContactDetector(baseline_period=10, contact_multiplier=cfg.get("contact_multiplier", 1.8), proximity_pct=self.buffer_factor * 2.0),
            "SENSEX": VolumeContactDetector(baseline_period=10, contact_multiplier=cfg.get("contact_multiplier", 1.8), proximity_pct=self.buffer_factor * 2.0)
        }
        self.spot_dryup_detectors: Dict[str, VolumeDryUpDetector] = {
            "NIFTY": VolumeDryUpDetector(lookback_period=10, min_dry_bars=cfg.get("min_dry_bars", 2), ignition_multiplier=cfg.get("ignition_multiplier", 1.8)),
            "SENSEX": VolumeDryUpDetector(lookback_period=10, min_dry_bars=cfg.get("min_dry_bars", 2), ignition_multiplier=cfg.get("ignition_multiplier", 1.8))
        }

        # Option volume tracking for RVOL calculation
        self.opt_5m_volumes: Dict[str, deque] = {} # token -> deque of volumes
        self.opt_aggregators: Dict[str, CandleAggregator] = {}


    def _get_range_limits(self, inst: str) -> tuple[float, float]:
        """Returns (min_range, max_range) appropriate for each index."""
        if inst == "SENSEX":
            return (60.0, 500.0)
        return (self.min_range, self.max_range)

    def _in_orb_window(self, dt: datetime) -> bool:
        t = dt.time()
        sh, sm, ss = map(int, self.orb_start_time.split(":"))
        eh, em, es = map(int, self.orb_end_time.split(":"))
        return time(sh, sm, ss) <= t <= time(eh, em, es)

    def _in_breakout_window(self, dt: datetime) -> bool:
        t = dt.time()
        sh, sm, ss = map(int, self.orb_end_time.split(":"))
        eh, em, es = map(int, self.end_time.split(":"))
        return time(sh, sm, ss) < t <= time(eh, em, es)

    def seed_from_spot(self, inst: str, spot_info: Dict[str, Any]):
        ltp = float(spot_info.get("ltp", 0.0))
        if ltp <= 0:
            return

        op = float(spot_info.get("open") or ltp)
        hi = float(spot_info.get("high") or ltp)
        lo = float(spot_info.get("low") or ltp)
        if op <= 0 or abs(op - ltp) / ltp > 0.08: op = ltp
        if hi <= 0 or hi < ltp or abs(hi - ltp) / ltp > 0.08: hi = max(ltp, op)
        if lo <= 0 or lo > ltp or abs(lo - ltp) / ltp > 0.08: lo = min(ltp, op)

        now_dt = self.parse_ist_time(datetime.now(timezone.utc).timestamp())
        t = now_dt.time()
        if t >= time(9, 30, 0):
            rng = self.orb_ranges.setdefault(inst, {"high": -1.0, "low": 1e9, "finalized": False})
            if rng.get("high", -1.0) <= 0:
                orb_h = max(op + (hi - op) * 0.5, op * 1.001)
                orb_l = min(op - (op - lo) * 0.5, op * 0.999)
                rng["high"] = round(orb_h, 2)
                rng["low"] = round(orb_l, 2)
                rng["finalized"] = True
                logger.info(f"⚡ [ORB_BREAKOUT] Pre-seeded {inst} ORB range: High={rng['high']}, Low={rng['low']}")

    async def on_tick(self, tick: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        raw_ts = tick.get("exchange_timestamp") or datetime.now(timezone.utc).timestamp()
        ts = float(raw_ts) if raw_ts > 1e11 else float(raw_ts)
        if ts > 1e11:
            ts /= 1000.0

        now_dt = self.parse_ist_time(ts)
        token = str(tick.get("token", ""))
        ltp = float(tick.get("ltp", 0.0))
        vol = float(tick.get("volume", 0.0))
        if ltp <= 0:
            return None

        # Spot tick processing
        if meta and meta.get("is_spot"):
            inst = meta.get("name", "NIFTY")
            if inst == "NIFTY" and (ltp < 15000.0 or ltp > 40000.0):
                return None
            if inst == "SENSEX" and (ltp < 50000.0 or ltp > 120000.0):
                return None

            today = now_dt.date()

            # Track new day session and gap
            dt_info = self.day_tracking[inst]
            if dt_info["date"] != today:
                dt_info["prev_close"] = ltp if dt_info["date"] is None else dt_info.get("last_close", ltp)
                dt_info["date"] = today
                dt_info["day_open"] = None
                dt_info["excessive_gap"] = False
                dt_info["gap_dir"] = None
                dt_info["gap_pct"] = 0.0
                self.orb_ranges[inst] = {"high": -1.0, "low": 1e9, "finalized": False, "pending_signal": None}

            dt_info["last_close"] = ltp

            # 1. Update ORB range during 09:15 - 09:30
            if self._in_orb_window(now_dt):
                if dt_info["day_open"] is None:
                    dt_info["day_open"] = ltp
                    if dt_info["prev_close"] and dt_info["prev_close"] > 0:
                        gap_diff = dt_info["day_open"] - dt_info["prev_close"]
                        gap_pct = abs(gap_diff) / dt_info["prev_close"] * 100.0
                        gap_dir = "UP" if gap_diff > 0 else "DOWN"
                        dt_info["gap_pct"] = gap_pct
                        dt_info["gap_dir"] = gap_dir

                        if gap_pct > self.extreme_gap_pct:
                            dt_info["excessive_gap"] = True
                            logger.warning(f"⚠️ [ORB GAP FILTER] {inst} opened with extreme {gap_pct:.2f}% gap (> {self.extreme_gap_pct}%). ORB Breakouts suppressed today.")
                        elif gap_pct > self.max_gap_pct:
                            logger.info(f"🚀 [ORB GAP-AND-GO] {inst} opened with {gap_pct:.2f}% gap {gap_dir} (> {self.max_gap_pct}%). Enabling Gap-and-Go continuation breakouts for {'CE' if gap_dir == 'UP' else 'PE'} only.")

                rng = self.orb_ranges[inst]
                if ltp > rng["high"]:
                    rng["high"] = ltp
                if ltp < rng["low"]:
                    rng["low"] = ltp
            elif self._in_breakout_window(now_dt):
                self.orb_ranges[inst]["finalized"] = True

            # 2. Update 5m Spot candle
            closed_spot_candle = self.spot_5m_aggregators[inst].on_tick(ts, ltp, volume=vol)
            if closed_spot_candle and self._in_breakout_window(now_dt) and self.orb_ranges[inst]["finalized"]:
                # Suppress only if opening gap was extreme (>1.50%)
                if dt_info.get("excessive_gap", False):
                    return None

                close_p = closed_spot_candle["close"]
                open_p = closed_spot_candle["open"]
                h_orb = self.orb_ranges[inst]["high"]
                l_orb = self.orb_ranges[inst]["low"]
                orb_range = h_orb - l_orb

                # Evaluate spot volume dynamics (Volume Dry-Up & Contact)
                v_dryup = self.spot_dryup_detectors[inst].update(closed_spot_candle)
                v_contact_h = self.spot_contact_detectors[inst].check_contact(closed_spot_candle, h_orb, "ORB_HIGH", direction="CE") if h_orb > 0 else {"is_contact": False, "boost_score": 0}
                v_contact_l = self.spot_contact_detectors[inst].check_contact(closed_spot_candle, l_orb, "ORB_LOW", direction="PE") if l_orb < 1e9 else {"is_contact": False, "boost_score": 0}

                if v_dryup["is_ignition"]:
                    logger.info(f"🔥 [ORB DRY-UP IGNITION] {inst} 5m spot breakout ignition after {v_dryup['dry_bars']} dry bars! Ratio: {v_dryup['ignition_ratio']:.2f}x")
                    self.emit_radar_alert(
                        alert_type="ORB_DRYUP_IGNITION",
                        instrument=inst,
                        direction="CE" if close_p >= ((h_orb + l_orb) / 2.0) else "PE",
                        title=f"🔥 {inst} ORB Breakout Ignition",
                        message=f"{inst} expanded after {v_dryup['dry_bars']} dry bars with {v_dryup['ignition_ratio']:.1f}x volume! Explosive expansion underway.",
                        meta_details=v_dryup,
                        now_ts=ts
                    )
                if v_contact_h.get("is_contact"):
                    logger.info(f"🎯 [ORB VOLUME CONTACT] {inst} contacted ORB High ({h_orb:.1f}) with {v_contact_h['vol_ratio']:.2f}x volume!")
                    self.emit_radar_alert(
                        alert_type="ORB_CONTACT",
                        instrument=inst,
                        direction="CE",
                        title=f"⚡ {inst} ORB High Volume Contact",
                        message=f"{inst} tested ORB High ({h_orb:.1f}) with {v_contact_h['vol_ratio']:.1f}x volume! Breakout pressure mounting.",
                        meta_details=v_contact_h,
                        now_ts=ts
                    )
                if v_contact_l.get("is_contact"):
                    logger.info(f"🎯 [ORB VOLUME CONTACT] {inst} contacted ORB Low ({l_orb:.1f}) with {v_contact_l['vol_ratio']:.2f}x volume!")
                    self.emit_radar_alert(
                        alert_type="ORB_CONTACT",
                        instrument=inst,
                        direction="PE",
                        title=f"⚡ {inst} ORB Low Volume Contact",
                        message=f"{inst} tested ORB Low ({l_orb:.1f}) with {v_contact_l['vol_ratio']:.1f}x volume! Breakdown pressure mounting.",
                        meta_details=v_contact_l,
                        now_ts=ts
                    )

                min_rng, max_rng = self._get_range_limits(inst)

                # Range quality filter (instrument-aware)
                if min_rng <= orb_range <= max_rng:
                    delta = close_p * self.buffer_factor
                    strength_factor = self.breakout_strength_pct / 100.0
                    ce_threshold = (h_orb + delta) * (1.0 + strength_factor)
                    pe_threshold = (l_orb - delta) * (1.0 - strength_factor)

                    gap_pct = dt_info.get("gap_pct", 0.0)
                    gap_dir = dt_info.get("gap_dir")

                    if h_orb > 0 and close_p > ce_threshold:
                        # If market opened with gap-down > max_gap_pct, do NOT take CE (avoid counter-trend trap)
                        if gap_pct > self.max_gap_pct and gap_dir == "DOWN":
                            logger.info(f"🚧 [ORB GAP FILTER] CE breakout suppressed: {inst} gap was -{gap_pct:.2f}% (counter-trend fade).")
                        else:
                            # Bullish Long Breakout with adaptive SL (entry - 0.5 * range)
                            adaptive_sl = close_p - (0.5 * orb_range)
                            self.orb_ranges[inst]["pending_signal"] = {
                                "direction": "CE",
                                "close": close_p,
                                "open": open_p,
                                "h_orb": h_orb,
                                "l_orb": l_orb,
                                "orb_range": orb_range,
                                "adaptive_sl": adaptive_sl,
                                "delta": delta,
                                "volume_contact": v_contact_h,
                                "volume_dryup": v_dryup,
                                "time": ts
                            }
                            self.emit_radar_alert(
                                alert_type="BREAKOUT_PENDING",
                                instrument=inst,
                                direction="CE",
                                title=f"🚀 {inst} ORB Breakout Imminent",
                                message=f"{inst} 5m candle closed above ORB High ({close_p:.1f} > {h_orb:.1f}). Confirming ATM CE option volume!",
                                now_ts=ts
                            )
                    elif l_orb < 1e9 and close_p < pe_threshold:
                        # If market opened with gap-up > max_gap_pct, do NOT take PE (avoid counter-trend trap)
                        if gap_pct > self.max_gap_pct and gap_dir == "UP":
                            logger.info(f"🚧 [ORB GAP FILTER] PE breakdown suppressed: {inst} gap was +{gap_pct:.2f}% (counter-trend fade).")
                        else:
                            # Bearish Short Breakdown (Gap-and-Go continuation!)
                            adaptive_sl = close_p + (0.5 * orb_range)
                            self.orb_ranges[inst]["pending_signal"] = {
                                "direction": "PE",
                                "close": close_p,
                                "open": open_p,
                                "h_orb": h_orb,
                                "l_orb": l_orb,
                                "orb_range": orb_range,
                                "adaptive_sl": adaptive_sl,
                                "delta": delta,
                                "volume_contact": v_contact_l,
                                "volume_dryup": v_dryup,
                                "time": ts
                            }
                            self.emit_radar_alert(
                                alert_type="BREAKOUT_PENDING",
                                instrument=inst,
                                direction="PE",
                                title=f"🚀 {inst} ORB Breakdown Imminent",
                                message=f"{inst} 5m candle closed below ORB Low ({close_p:.1f} < {l_orb:.1f}). Confirming ATM PE option volume!",
                                now_ts=ts
                            )

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

        # Track 5m option volume for RVOL
        if token not in self.opt_aggregators:
            self.opt_aggregators[token] = CandleAggregator(timeframe_seconds=300)
            self.opt_5m_volumes[token] = deque(maxlen=20)

        closed_opt_candle = self.opt_aggregators[token].on_tick(ts, ltp, volume=vol)
        if closed_opt_candle:
            self.opt_5m_volumes[token].append(closed_opt_candle.get("volume", 0.0))

        # Check if there is a pending breakout on Spot that matches this ATM option
        pending = self.orb_ranges.get(inst, {}).get("pending_signal")
        if pending and is_atm and pending["direction"] == opt_type:
            vols = list(self.opt_5m_volumes.get(token, []))
            rvol = 1.85 # Default pass if early in session
            if len(vols) >= 3:
                avg_vol = sum(vols) / len(vols)
                rvol = (closed_opt_candle["volume"] / avg_vol) if (closed_opt_candle and avg_vol > 0) else 1.85

            if rvol >= self.min_rvol:
                min_rng, max_rng = self._get_range_limits(inst)
                # Compute confidence score
                conditions = {
                    "trend_alignment": (pending["close"] > pending["open"]) if opt_type == "CE" else (pending["close"] < pending["open"]),
                    "volume_confirmation": rvol >= self.min_rvol,
                    "momentum_strength": min(1.0, abs(pending["close"] - (pending["h_orb"] if opt_type == "CE" else pending["l_orb"])) / max(10.0, pending["orb_range"] * 0.3)),
                    "time_quality": self.is_time_gated(now_dt, "09:30:00", "10:15:00"),
                    "context_filter": (min_rng <= pending["orb_range"] <= max_rng),
                    "volume_contact_bonus": pending.get("volume_contact", {}).get("boost_score", 0),
                    "volume_dryup_bonus": pending.get("volume_dryup", {}).get("boost_score", 0)
                }
                confidence = self.compute_confidence(conditions)

                if confidence < self.confidence_threshold:
                    logger.warning(f"⚠️ [ORB SUPPRESSED] {symbol} ({opt_type}) score {confidence} < threshold {self.confidence_threshold}. Conditions: {conditions}")
                    return None

                sig_key = f"ORB_{inst}_{opt_type}_{strike}"
                if self.can_trigger(sig_key, ts):
                    self.orb_ranges[inst]["pending_signal"] = None
                    logger.info(f"⚡ [ORB BREAKOUT] Triggered for {symbol} ({opt_type})! RVOL: {rvol:.2f}, Confidence: {confidence}%, Spot: {pending['close']}")

                    return self.build_signal_payload(
                        instrument=inst,
                        direction=opt_type,
                        strike=strike,
                        option_type=opt_type,
                        option_token=token,
                        option_symbol=symbol,
                        spot_entry=pending["close"],
                        entry_price=ltp,
                        lot_size=lot_size,
                        confidence=confidence,
                        custom_sl_spot=pending.get("adaptive_sl"),
                        meta_details={
                            "rvol": round(rvol, 2),
                            "h_orb": pending.get("h_orb"),
                            "l_orb": pending.get("l_orb"),
                            "orb_range": round(pending.get("orb_range", 0.0), 1),
                            "adaptive_sl": round(pending.get("adaptive_sl", 0.0), 1),
                            "delta": round(pending.get("delta", 0.0), 2),
                            "confidence": confidence,
                            "volume_contact": pending.get("volume_contact"),
                            "volume_dryup": pending.get("volume_dryup")
                        }
                    )
        return None
