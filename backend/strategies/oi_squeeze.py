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

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(name="OI_SQUEEZE")
        cfg = config or {}
        self.start_time = cfg.get("start_time", "09:15:00")
        self.end_time = cfg.get("end_time", "15:30:00")
        self.tau = cfg.get("lookback_window_sec", 300) # 300 seconds (5 min)
        self.min_oi_drop_pct = cfg.get("min_oi_drop_pct", -5.0) # -5.0%
        self.min_price_spike_pct = cfg.get("min_price_spike_pct", 3.0) # +3.0%
        self.vol_multiplier = cfg.get("vol_multiplier", 2.0)
        self.min_adx = cfg.get("min_adx", 20.0)

        # Rolling history per option token: deque of (ts, ltp, oi, cumulative_vol)
        self.token_history: Dict[str, deque] = {}
        # 1-min aggregators per option token
        self.aggregators: Dict[str, CandleAggregator] = {}
        # 1-min candle volume history (last 20 candles) per option token
        self.volume_history_1m: Dict[str, deque] = {}

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
        if instrument in self.spot_ema20:
            self.spot_ema20[instrument].update(price)
        if instrument in self.spot_aggregators:
            closed = self.spot_aggregators[instrument].on_tick(ts, price, vol)
            if closed and instrument in self.spot_adx:
                self.spot_adx[instrument].update(closed["high"], closed["low"], closed["close"])

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
        logger.info(f"✅ [OI_SQUEEZE] {inst} warmed up! EMA20={self.spot_ema20[inst].value:.1f}, ADX={self.spot_adx[inst].value:.1f}")

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

        # Compare oldest in window vs latest
        old_ts, old_ltp, old_oi, old_vol = hist[0]
        time_span = ts - old_ts

        # Require meaningful baseline OI
        if old_oi <= 100 or old_ltp <= 0.5:
            return None

        delta_oi_pct = ((oi - old_oi) / old_oi) * 100.0
        delta_price_pct = ((ltp - old_ltp) / old_ltp) * 100.0

        # Dynamic Warm-up Guard:
        # Standard squeezes require >= 120s and >= 8 ticks.
        # High-velocity squeezes (dOI <= -8% and dP >= 5%) need only >= 60s and >= 5 ticks.
        is_extreme_squeeze = (delta_oi_pct <= -8.0 and delta_price_pct >= 5.0)
        min_required_time = 60.0 if is_extreme_squeeze else 120.0
        min_ticks = 5 if is_extreme_squeeze else 8

        if len(hist) < min_ticks or time_span < min_required_time:
            return None

        if delta_oi_pct <= -4.0 or delta_price_pct >= 2.5:
            logger.debug(f"[OI DEBUG] {token} ({symbol}): dOI={delta_oi_pct:.2f}%, dP={delta_price_pct:.2f}%, span={time_span:.0f}s, old_oi={old_oi}, cur_oi={oi}, old_ltp={old_ltp}, cur_ltp={ltp}")

        # Check conditions
        # 1. Delta OI <= -5.0% (Unwinding)
        # 2. Delta Price >= +3.0%
        if delta_oi_pct > self.min_oi_drop_pct or delta_price_pct < self.min_price_spike_pct:
            return None

        # 3. Spot vs EMA20
        spot = self.spot_prices.get(inst, 0.0)
        ema20 = self.spot_ema20.get(inst).value if self.spot_ema20.get(inst) else None
        
        # Directional validation:
        # If EMA20 is available, verify directional alignment. If in extreme squeeze, allow slight tolerance.
        if ema20 and spot > 0:
            is_bullish_ce = (opt_type == "CE" and spot >= ema20 * 0.9995)
            is_bearish_pe = (opt_type == "PE" and spot <= ema20 * 1.0005)
            if not (is_bullish_ce or is_bearish_pe):
                return None
        elif spot <= 0:
            return None

        # 4. ADX Filter: Ensure market is trending (ADX >= 20)
        # Compression Exception: When a violent breakout launches out of a tight range,
        # ADX is initially low (<20) because the compression is just ending.
        # High-conviction squeezes are not discarded on low ADX.
        adx_val = self.spot_adx.get(inst, IncrementalADX()).value
        if adx_val < self.min_adx and not is_extreme_squeeze:
            return None

        # 5. Volume surge check: Option vol in tau >= 2.0x avg 1m volume
        vols = list(self.volume_history_1m.get(token, []))
        vol_confirmed = True
        if len(vols) >= 5:
            avg_vol = sum(vols) / len(vols)
            window_vol = vol - old_vol
            if avg_vol > 0 and window_vol < (self.vol_multiplier * avg_vol):
                return None
            vol_confirmed = (avg_vol > 0 and window_vol >= (self.vol_multiplier * avg_vol))

        # 6. Confidence Scoring
        conditions = {
            "trend_alignment": True,
            "volume_confirmation": vol_confirmed,
            "momentum_strength": min(1.0, delta_price_pct / 6.0),
            "time_quality": self.is_time_gated(now_dt, "09:30:00", "15:00:00"),
            "context_filter": (adx_val >= 25.0)
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

        logger.info(f"⚡ [OI SQUEEZE] Triggered for {symbol} ({opt_type})! dOI: {delta_oi_pct:.1f}%, dPrice: +{delta_price_pct:.1f}%, Confidence: {confidence}%, ADX: {adx_val:.1f}")

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
            meta_details={
                "delta_oi_pct": round(delta_oi_pct, 2),
                "delta_price_pct": round(delta_price_pct, 2),
                "spot": spot,
                "spot_ema20": round(ema20, 2),
                "adx": round(adx_val, 1),
                "lookback_tau_sec": self.tau,
                "confidence": confidence
            }
        )
