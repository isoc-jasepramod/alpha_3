"""
PROJECT ALPHA 3.0 — SIMPLIFIED VWAP & EMA PRICE ACTION ENGINE
=============================================================
A streamlined, high-conviction strategy engine operating directly on pure Spot 
VWAP & EMA Price Action and ATM Option Execution, protected by an Option RVOL Gate.

Core Rules:
1. 3-Minute VWAP & EMA Rejection Pullbacks (09:30 - 15:00 IST):
   - Trend Alignment:
     * CE: EMA9 > EMA21, Spot Close > Session VWAP, ADX >= 22.0, RSI [48 - 72]
     * PE: EMA9 < EMA21, Spot Close < Session VWAP, ADX >= 22.0, RSI [18 - 52]
   - Rejection Wick:
     * CE: Bottom wick ratio >= 0.35 (buyer defense of lower prices at EMA/VWAP)
     * PE: Top wick ratio >= 0.15 (seller defense of higher prices at EMA/VWAP)
   - Confirmation Candle:
     * CE: Subsequent 3-min candle closes ABOVE EMA9 with green body (c >= o)
     * PE: Subsequent 3-min candle closes BELOW EMA9 with red body (c <= o)
   - Spot SL:
     * CE: min(rejection_low, confirmation_low, EMA9) - 5.0 pts
     * PE: max(rejection_high, confirmation_high, EMA9) + 5.0 pts

2. Option RVOL Gate:
   - Accumulates 3-minute option volume deltas.
   - When a spot setup is confirmed, requires Option RVOL >= min_opt_rvol (default 1.20x)
     to verify institutional / market-wide volume participation.

3. Option Mapping & Risk Governor Integration:
   - When confirmed and RVOL passed, executes on the ATM Option contract.
   - Routes candidate payload with custom_sl_spot to RiskGovernor for synthetic option SL,
     1:2 R/R targets, 1% equity sizing, and hard 20% option SL floor.
"""

from collections import deque
from datetime import datetime, timezone, time
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
    calculate_top_wick_ratio
)


class SimplifiedPriceActionEngine(BaseStrategy):
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(name="SIMPLIFIED_VWAP_EMA")
        cfg = config or {}

        self.start_time = cfg.get("start_time", "09:15:00")
        self.end_time = cfg.get("end_time", "15:15:00")
        self.cooldown_sec = float(cfg.get("cooldown_sec", 180.0))

        # Filter thresholds
        self.min_adx = float(cfg.get("min_adx", 22.0))
        self.rsi_ce_min = float(cfg.get("rsi_ce_min", 50.0))
        self.rsi_ce_max = float(cfg.get("rsi_ce_max", 66.0))
        self.rsi_pe_min = float(cfg.get("rsi_pe_min", 34.0))
        self.rsi_pe_max = float(cfg.get("rsi_pe_max", 50.0))
        self.min_bottom_wick = float(cfg.get("min_bottom_wick_ratio", 0.35))
        self.min_top_wick = float(cfg.get("min_top_wick_ratio", 0.15))

        # Rubberband overextension guard (max allowed distance between spot close and EMA21)
        self.max_ema21_dist_nifty = float(cfg.get("max_ema21_dist_nifty", 35.0))
        self.max_ema21_dist_sensex = float(cfg.get("max_ema21_dist_sensex", 100.0))

        # Directional consecutive stop pause
        self.max_consecutive_stops_dir = int(cfg.get("max_consecutive_stops_dir", 2))
        self.stop_pause_sec = float(cfg.get("stop_pause_sec", 900.0))
        self._consecutive_stops: Dict[str, int] = {}
        self._pause_until: Dict[str, float] = {}

        # Option RVOL Gate parameters
        self.min_opt_rvol = float(cfg.get("min_opt_rvol", 1.20))
        self.require_opt_rvol = bool(cfg.get("require_opt_rvol", True))

        # Option volume trackers for RVOL computation
        self.opt_3m_aggregators: Dict[str, CandleAggregator] = {}
        self.opt_3m_vols: Dict[str, deque] = {}
        self.opt_prev_cum_vol: Dict[str, float] = {}

        # Spot tracking per instrument (NIFTY & SENSEX)
        self.spot_prices: Dict[str, float] = {"NIFTY": 0.0, "SENSEX": 0.0}

        # 3-minute Spot aggregators & indicators for VWAP/EMA Pullback
        self.spot_3m_aggregators: Dict[str, CandleAggregator] = {
            "NIFTY": CandleAggregator(timeframe_seconds=180),
            "SENSEX": CandleAggregator(timeframe_seconds=180)
        }
        self.spot_ema9: Dict[str, IncrementalEMA] = {
            "NIFTY": IncrementalEMA(period=9),
            "SENSEX": IncrementalEMA(period=9)
        }
        self.spot_ema21: Dict[str, IncrementalEMA] = {
            "NIFTY": IncrementalEMA(period=21),
            "SENSEX": IncrementalEMA(period=21)
        }
        self.spot_vwap: Dict[str, IncrementalVWAP] = {
            "NIFTY": IncrementalVWAP(),
            "SENSEX": IncrementalVWAP()
        }
        self.spot_adx: Dict[str, IncrementalADX] = {
            "NIFTY": IncrementalADX(period=14),
            "SENSEX": IncrementalADX(period=14)
        }
        self.spot_rsi: Dict[str, IncrementalRSI] = {
            "NIFTY": IncrementalRSI(period=14),
            "SENSEX": IncrementalRSI(period=14)
        }

        # Pullback state machine per instrument
        # awaiting_confirmation: set when rejection candle forms, awaiting green/red confirmation
        self.awaiting_confirmation: Dict[str, Optional[Dict[str, Any]]] = {
            "NIFTY": None,
            "SENSEX": None
        }
        # pending_setup: set when confirmation closes, waiting for option tick with RVOL to execute
        self.pending_setup: Dict[str, Optional[Dict[str, Any]]] = {
            "NIFTY": None,
            "SENSEX": None
        }

    def seed_from_candles(self, inst: str, candles: List[Dict[str, Any]]):
        """Pre-warms 3-minute indicators from historical prior-session candles."""
        if not candles:
            return
        logger.info(f"🔄 [SIMPLIFIED_VWAP_EMA] Warming up {inst} with {len(candles)} historical candles...")
        for c in candles:
            h = float(c.get("high", 0.0))
            l = float(c.get("low", 0.0))
            cl = float(c.get("close", 0.0))
            if cl <= 0:
                continue

            self.spot_prices[inst] = cl
            if inst in self.spot_ema9:
                self.spot_ema9[inst].update(cl)
            if inst in self.spot_ema21:
                self.spot_ema21[inst].update(cl)
            if inst in self.spot_adx:
                self.spot_adx[inst].update(h, l, cl)
            if inst in self.spot_rsi:
                self.spot_rsi[inst].update(cl)

        # Session VWAP is strictly intraday (09:15 onwards)
        if inst in self.spot_vwap:
            self.spot_vwap[inst].reset()

        def _f(val):
            return f"{val:.2f}" if val is not None else "None"

        logger.info(
            f"✅ [SIMPLIFIED_VWAP_EMA] {inst} warmed! "
            f"EMA9={_f(self.spot_ema9[inst].value)}, EMA21={_f(self.spot_ema21[inst].value)}, "
            f"ADX={_f(self.spot_adx[inst].value)} (VWAP fresh for current session)"
        )

    def seed_from_spot(self, inst: str, spot_info: Dict[str, Any]):
        """Fallback spot seed only if historical candles were not available."""
        ltp = float(spot_info.get("ltp", 0.0))
        if ltp > 0:
            self.spot_prices[inst] = ltp
            if inst in self.spot_ema9 and self.spot_ema9[inst].value is None:
                self.spot_ema9[inst].seed(ltp)
            if inst in self.spot_ema21 and self.spot_ema21[inst].value is None:
                self.spot_ema21[inst].seed(ltp)
            # Session VWAP is strictly intraday starting from 09:15 AM; do not inject artificial 10k volume

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
        vol = float(tick.get("volume", 0.0))
        if ltp <= 0:
            return None

        # =========================================================================
        # 1. SPOT TICK PROCESSING (Candle Aggregation & Setup Detection)
        # =========================================================================
        if meta and meta.get("is_spot"):
            inst = meta.get("name", "NIFTY")
            self.spot_prices[inst] = ltp

            # 3-Min Aggregator: VWAP & EMA Pullback Rejection
            closed_3m = self.spot_3m_aggregators[inst].on_tick(ts, ltp, volume=1.0)
            if closed_3m:
                self.pending_setup[inst] = None  # Clear any stale pending setup from prior bar
                o = closed_3m["open"]
                h = closed_3m["high"]
                l = closed_3m["low"]
                c = closed_3m["close"]

                e9 = self.spot_ema9[inst].update(c)
                e21 = self.spot_ema21[inst].update(c)
                v = self.spot_vwap[inst].update((h + l + c) / 3.0, 1000.0)
                adx = self.spot_adx[inst].update(h, l, c)
                rsi = self.spot_rsi[inst].update(c)

                bot_wick = calculate_bottom_wick_ratio(o, h, l, c)
                top_wick = calculate_top_wick_ratio(o, h, l, c)

                # Step 1: Check if prior candle was awaiting confirmation
                awaiting = self.awaiting_confirmation[inst]
                if awaiting:
                    aw_dir = awaiting["direction"]
                    sig_key = f"SIMPLIFIED_VWAP_{inst}_{aw_dir}"

                    # Bullish Confirmation: Next candle closes above EMA9 and is green
                    if aw_dir == "CE" and c > e9 and c >= o:
                        if self.can_trigger(sig_key, ts):
                            custom_sl = min(awaiting["low"], l, e9) - 5.0
                            logger.info(f"🎯 [SIMPLIFIED VWAP_EMA] {inst} Bullish Pullback Confirmed at {c:.2f}! Arming CE entry...")
                            self.pending_setup[inst] = {
                                "direction": "CE",
                                "spot": c,
                                "custom_sl": custom_sl,
                                "time": ts,
                                "type": "VWAP_PULLBACK"
                            }
                    # Bearish Confirmation: Next candle closes below EMA9 and is red
                    elif aw_dir == "PE" and c < e9 and c <= o:
                        if self.can_trigger(sig_key, ts):
                            custom_sl = max(awaiting["high"], h, e9) + 5.0
                            logger.info(f"🎯 [SIMPLIFIED VWAP_EMA] {inst} Bearish Breakdown Confirmed at {c:.2f}! Arming PE entry...")
                            self.pending_setup[inst] = {
                                "direction": "PE",
                                "spot": c,
                                "custom_sl": custom_sl,
                                "time": ts,
                                "type": "VWAP_PULLBACK"
                            }
                    self.awaiting_confirmation[inst] = None

                # Step 2: Check for fresh rejection setup (protected by Rubberband Guard & Directional Pause)
                t_str_3m = now_dt.strftime("%H:%M:%S")
                max_dist = self.max_ema21_dist_nifty if inst == "NIFTY" else self.max_ema21_dist_sensex
                dist21 = abs(c - e21) if (e21 is not None) else 0.0

                if "09:30:00" <= t_str_3m <= "15:00:00" and adx >= self.min_adx and dist21 <= max_dist:
                    pause_ce = ts < self._pause_until.get(f"{inst}_CE", 0.0)
                    pause_pe = ts < self._pause_until.get(f"{inst}_PE", 0.0)

                    # Bullish Rejection: Trend is UP (EMA9 > EMA21, c > VWAP), RSI healthy, bottom wick rejection
                    if not pause_ce and e9 and e21 and e9 > e21 and c > v and bot_wick >= self.min_bottom_wick:
                        if self.rsi_ce_min <= rsi <= self.rsi_ce_max:
                            logger.info(
                                f"👀 [SIMPLIFIED VWAP_EMA] {inst} Bullish Pullback Wick Detected @ {c:.2f} "
                                f"(bot_wick={bot_wick:.2f}, RSI={rsi:.1f}, ADX={adx:.1f}). Awaiting next green candle above EMA9..."
                            )
                            self.awaiting_confirmation[inst] = {
                                "direction": "CE",
                                "low": l,
                                "high": h,
                                "time": ts
                            }
                    # Bearish Rejection: Trend is DOWN (EMA9 < EMA21, c < VWAP), RSI healthy, top wick rejection
                    elif not pause_pe and e9 and e21 and e9 < e21 and c < v and top_wick >= self.min_top_wick:
                        if self.rsi_pe_min <= rsi <= self.rsi_pe_max:
                            logger.info(
                                f"👀 [SIMPLIFIED VWAP_EMA] {inst} Bearish Rejection Wick Detected @ {c:.2f} "
                                f"(top_wick={top_wick:.2f}, RSI={rsi:.1f}, ADX={adx:.1f}). Awaiting next red candle below EMA9..."
                            )
                            self.awaiting_confirmation[inst] = {
                                "direction": "PE",
                                "low": l,
                                "high": h,
                                "time": ts
                            }

            return None

        # =========================================================================
        # 2. OPTION TICK PROCESSING (Volume Tracking & Execution on ATM Contract)
        # =========================================================================
        if not meta or not meta.get("option_type"):
            return None

        inst = meta.get("name", "NIFTY")
        opt_type = meta.get("option_type")
        strike = float(meta.get("strike", 0.0))
        lot_size = int(meta.get("lot_size", 50))
        symbol = meta.get("symbol", "")

        # Track cumulative option volume -> per-tick delta for Option RVOL
        if token not in self.opt_3m_aggregators:
            self.opt_3m_aggregators[token] = CandleAggregator(timeframe_seconds=180)
            self.opt_3m_vols[token] = deque(maxlen=20)
        prev_cum = self.opt_prev_cum_vol.get(token)
        delta_vol = 0.0 if prev_cum is None else max(0.0, vol - prev_cum)
        self.opt_prev_cum_vol[token] = vol
        closed_opt = self.opt_3m_aggregators[token].on_tick(ts, ltp, volume=delta_vol)
        if closed_opt:
            self.opt_3m_vols[token].append(closed_opt.get("volume", 0.0))

        pending = self.pending_setup.get(inst)
        if not pending:
            return None

        # Expire pending setup if older than 45 seconds, or ignore out-of-order past ticks
        age = ts - pending["time"]
        if age < 0:
            return None
        if age > 45.0:
            logger.info(f"⌛ [SIMPLIFIED VWAP_EMA] {inst} pending setup expired after {age:.1f}s without option RVOL.")
            self.pending_setup[inst] = None
            return None

        # Check if this option matches the direction (CE or PE)
        if pending["direction"] != opt_type:
            return None

        # Real-time Spot Invalidation Check: Did market reverse past stop loss or run away in opposite direction?
        current_spot = self.spot_prices.get(inst, 0.0)
        custom_sl = pending["custom_sl"]
        p_dir = pending["direction"]
        if current_spot > 0:
            spot_buffer = 15.0 if inst == "NIFTY" else 50.0
            if p_dir == "PE" and (current_spot >= custom_sl or current_spot > pending["spot"] + spot_buffer):
                logger.warning(
                    f"❌ [SIMPLIFIED VWAP_EMA INVALIDATED] {inst} PE setup aborted! Spot moved to {current_spot:.1f} "
                    f"(breached stop level {custom_sl:.1f} / setup spot {pending['spot']:.1f}). Market reversed."
                )
                self.pending_setup[inst] = None
                return None
            elif p_dir == "CE" and (current_spot <= custom_sl or current_spot < pending["spot"] - spot_buffer):
                logger.warning(
                    f"❌ [SIMPLIFIED VWAP_EMA INVALIDATED] {inst} CE setup aborted! Spot moved to {current_spot:.1f} "
                    f"(breached stop level {custom_sl:.1f} / setup spot {pending['spot']:.1f}). Market reversed."
                )
                self.pending_setup[inst] = None
                return None

        # Check if option is ATM or near-ATM (offset 0 or within 1 strike, or closest lake surrogate)
        offset = meta.get("offset")
        is_atm = (abs(offset) <= 1) if offset is not None else True
        if not is_atm and not meta.get("is_closest_lake"):
            spot = self.spot_prices.get(inst, 0.0)
            step = 50.0 if inst == "NIFTY" else 100.0
            if spot > 0 and abs(strike - spot) > (step * 1.5):
                return None

        # -------------------------------------------------------------
        # Option RVOL Gate Validation
        # -------------------------------------------------------------
        ov = list(self.opt_3m_vols.get(token, []))
        option_rvol = None
        if len(ov) >= 2:
            baseline = sum(ov[:-1]) / (len(ov) - 1)
            if baseline > 0:
                option_rvol = ov[-1] / baseline

        # If baseline exists and Option RVOL is below threshold, filter out low-volume traps
        if option_rvol is not None and option_rvol < self.min_opt_rvol:
            logger.info(
                f"🚧 [SIMPLIFIED VWAP_EMA] {symbol} ({opt_type}) held: Option RVOL "
                f"{option_rvol:.2f} < {self.min_opt_rvol} (insufficient option volume surge)."
            )
            return None

        # Setup Confirmed & Volume Validated! Clear pending setup so we don't trigger twice
        setup_type = pending["type"]
        spot_entry = current_spot if current_spot > 0 else pending["spot"]
        custom_sl = pending["custom_sl"]
        self.pending_setup[inst] = None

        rvol_str = f"{option_rvol:.2f}x" if option_rvol is not None else "warming"
        logger.info(
            f"⚡ [SIMPLIFIED VWAP_EMA TRIGGER] {setup_type} | {symbol} ({opt_type})! "
            f"Entry: ₹{ltp:.2f} | OptRVOL: {rvol_str} | Spot: {spot_entry:.1f} | Custom Spot SL: {custom_sl:.1f}"
        )

        meta_details = {
            "setup_type": setup_type,
            "spot_entry": spot_entry,
            "custom_sl_spot": custom_sl,
            "lot_size": lot_size,
            "option_rvol": round(option_rvol, 2) if option_rvol is not None else None
        }

        return self.build_signal_payload(
            instrument=inst,
            direction=opt_type,
            strike=strike,
            option_type=opt_type,
            option_token=token,
            option_symbol=symbol,
            spot_entry=spot_entry,
            entry_price=ltp,
            lot_size=lot_size,
            confidence=85,
            custom_sl_spot=custom_sl,
            meta_details=meta_details
        )

    def notify_resolution(self, signal: Dict[str, Any]):
        """
        Called when a trade resolves. If consecutive stop losses hit in the same direction,
        pauses that direction for stop_pause_sec (15 min).
        """
        inst = signal.get("instrument", "NIFTY")
        direction = signal.get("direction", "PE")
        status = signal.get("exit_reason", "")
        key = f"{inst}_{direction}"
        now_ts = signal.get("exit_ts") or datetime.now(timezone.utc).timestamp()

        if "STOP" in status:
            self._consecutive_stops[key] = self._consecutive_stops.get(key, 0) + 1
            if self._consecutive_stops[key] >= self.max_consecutive_stops_dir:
                self._pause_until[key] = now_ts + self.stop_pause_sec
                logger.warning(
                    f"⏸️ [SIMPLIFIED VWAP_EMA] {key} paused until {self.stop_pause_sec/60:.0f}m after "
                    f"{self._consecutive_stops[key]} consecutive stops."
                )
        elif "TARGET" in status:
            self._consecutive_stops[key] = 0

