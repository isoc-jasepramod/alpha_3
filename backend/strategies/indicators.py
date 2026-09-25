import math
from collections import deque
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

class IncrementalEMA:
    """Computes Exponential Moving Average incrementally with O(1) time complexity."""
    def __init__(self, period: int):
        self.period = period
        self.multiplier = 2.0 / (period + 1)
        self.ema: Optional[float] = None
        self.count = 0
        self._initial_sum = 0.0

    def update(self, price: float) -> float:
        self.count += 1
        if self.ema is None:
            self._initial_sum += price
            if self.count >= self.period:
                self.ema = self._initial_sum / self.period
            return price
        
        self.ema = (price - self.ema) * self.multiplier + self.ema
        return self.ema

    def seed(self, initial_value: float):
        self.ema = float(initial_value)
        self.count = self.period
        self._initial_sum = float(initial_value) * self.period

    @property
    def value(self) -> Optional[float]:
        return self.ema


class IncrementalVWAP:
    """Computes session Volume-Weighted Average Price incrementally."""
    def __init__(self):
        self.cumulative_pv = 0.0
        self.cumulative_vol = 0.0

    def update(self, price: float, volume: float) -> float:
        self.cumulative_pv += price * volume
        self.cumulative_vol += volume
        if self.cumulative_vol > 0:
            return self.cumulative_pv / self.cumulative_vol
        return price

    @property
    def value(self) -> float:
        if self.cumulative_vol > 0:
            return self.cumulative_pv / self.cumulative_vol
        return 0.0

    def seed(self, initial_vwap: float, initial_vol: float = 100000.0):
        self.cumulative_pv = float(initial_vwap) * float(initial_vol)
        self.cumulative_vol = float(initial_vol)

    def reset(self):
        self.cumulative_pv = 0.0
        self.cumulative_vol = 0.0


class IncrementalRSI:
    """Computes Relative Strength Index (RSI) using Wilder's smoothing."""
    def __init__(self, period: int = 14):
        self.period = period
        self.prev_close: Optional[float] = None
        self.avg_gain: Optional[float] = None
        self.avg_loss: Optional[float] = None
        self.count = 0
        self.gains: List[float] = []
        self.losses: List[float] = []

    def update(self, close: float) -> float:
        if self.prev_close is None:
            self.prev_close = close
            return 50.0

        change = close - self.prev_close
        self.prev_close = close
        gain = max(change, 0.0)
        loss = max(-change, 0.0)

        if self.avg_gain is None:
            self.gains.append(gain)
            self.losses.append(loss)
            self.count += 1
            if self.count >= self.period:
                self.avg_gain = sum(self.gains) / self.period
                self.avg_loss = sum(self.losses) / self.period
                rs = self.avg_gain / (self.avg_loss if self.avg_loss > 0 else 1e-6)
                return 100.0 - (100.0 / (1.0 + rs))
            return 50.0

        self.avg_gain = (self.avg_gain * (self.period - 1) + gain) / self.period
        self.avg_loss = (self.avg_loss * (self.period - 1) + loss) / self.period
        rs = self.avg_gain / (self.avg_loss if self.avg_loss > 0 else 1e-6)
        return 100.0 - (100.0 / (1.0 + rs))

    def seed(self, initial_rsi: float = 50.0, reference_price: float = 0.0):
        self.prev_close = float(reference_price) if reference_price > 0 else None
        self.count = self.period
        clamped = max(5.0, min(95.0, float(initial_rsi)))
        rs = clamped / (100.0 - clamped)
        self.avg_loss = 10.0
        self.avg_gain = 10.0 * rs

    @property
    def value(self) -> float:
        if self.avg_gain is None or self.avg_loss is None:
            return 50.0
        rs = self.avg_gain / (self.avg_loss if self.avg_loss > 0 else 1e-6)
        return 100.0 - (100.0 / (1.0 + rs))


class IncrementalATR:
    """Computes Average True Range over N periods."""
    def __init__(self, period: int = 14):
        self.period = period
        self.prev_close: Optional[float] = None
        self.atr: Optional[float] = None
        self.tr_history = deque(maxlen=period)

    def update(self, high: float, low: float, close: float) -> float:
        if self.prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
        
        self.prev_close = close
        self.tr_history.append(tr)

        if len(self.tr_history) < self.period:
            return sum(self.tr_history) / len(self.tr_history)

        if self.atr is None:
            self.atr = sum(self.tr_history) / self.period
        else:
            self.atr = (self.atr * (self.period - 1) + tr) / self.period
        return self.atr

    def seed(self, initial_atr: float, reference_close: float = 0.0):
        self.atr = float(initial_atr)
        self.prev_close = float(reference_close) if reference_close > 0 else None
        self.tr_history = deque([float(initial_atr)] * self.period, maxlen=self.period)

    @property
    def value(self) -> float:
        return self.atr if self.atr is not None else 10.0


class IncrementalADX:
    """Computes Average Directional Index (ADX) using Wilder's smoothing."""
    def __init__(self, period: int = 14):
        self.period = period
        self.prev_high: Optional[float] = None
        self.prev_low: Optional[float] = None
        self.prev_close: Optional[float] = None
        self.tr_smooth: Optional[float] = None
        self.plus_dm_smooth: Optional[float] = None
        self.minus_dm_smooth: Optional[float] = None
        self.adx: Optional[float] = None
        self.dx_history: deque = deque(maxlen=period)
        self.count = 0

    def seed(self, initial_adx: float = 25.0):
        self.adx = float(initial_adx)
        self.count = self.period
        self.dx_history = deque([float(initial_adx)] * self.period, maxlen=self.period)

    def update(self, high: float, low: float, close: float) -> float:
        if self.prev_high is None or self.prev_low is None or self.prev_close is None:
            self.prev_high = high
            self.prev_low = low
            self.prev_close = close
            return 20.0

        tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
        up_move = high - self.prev_high
        down_move = self.prev_low - low

        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0

        self.prev_high = high
        self.prev_low = low
        self.prev_close = close
        self.count += 1

        if self.tr_smooth is None:
            self.tr_smooth = tr
            self.plus_dm_smooth = plus_dm
            self.minus_dm_smooth = minus_dm
        else:
            self.tr_smooth = (self.tr_smooth * (self.period - 1) + tr) / self.period
            self.plus_dm_smooth = (self.plus_dm_smooth * (self.period - 1) + plus_dm) / self.period
            self.minus_dm_smooth = (self.minus_dm_smooth * (self.period - 1) + minus_dm) / self.period

        tr_denom = self.tr_smooth if self.tr_smooth > 1e-6 else 1e-6
        plus_di = 100.0 * (self.plus_dm_smooth / tr_denom)
        minus_di = 100.0 * (self.minus_dm_smooth / tr_denom)
        di_sum = plus_di + minus_di
        dx = 100.0 * (abs(plus_di - minus_di) / (di_sum if di_sum > 1e-6 else 1e-6))

        self.dx_history.append(dx)

        if self.adx is None:
            if len(self.dx_history) >= self.period:
                self.adx = sum(self.dx_history) / self.period
            else:
                return dx
        else:
            self.adx = (self.adx * (self.period - 1) + dx) / self.period

        return self.adx

    @property
    def value(self) -> float:
        return self.adx if self.adx is not None else 20.0


def calculate_bottom_wick_ratio(open_p: float, high_p: float, low_p: float, close_p: float) -> float:
    """
    Bottom Wick Rejection Ratio = (min(Open, Close) - Low) / (High - Low)
    Clamped against zero-division for dojis.
    """
    range_p = high_p - low_p
    if range_p <= 1e-5:
        return 0.0
    body_low = min(open_p, close_p)
    return max(0.0, (body_low - low_p) / range_p)


def calculate_top_wick_ratio(open_p: float, high_p: float, low_p: float, close_p: float) -> float:
    """
    Top Wick Rejection Ratio = (High - max(Open, Close)) / (High - Low)
    """
    range_p = high_p - low_p
    if range_p <= 1e-5:
        return 0.0
    body_high = max(open_p, close_p)
    return max(0.0, (high_p - body_high) / range_p)


class CandleAggregator:
    """
    Aggregates incoming ticks into fixed-timeframe OHLCV candles (e.g. 1m, 3m, 5m).
    """
    def __init__(self, timeframe_seconds: int = 60):
        self.timeframe = timeframe_seconds
        self.current_candle: Optional[Dict[str, Any]] = None
        self.closed_candles: deque = deque(maxlen=200)

    def on_tick(self, timestamp: float, price: float, volume: float = 1.0) -> Optional[Dict[str, Any]]:
        """
        Returns a closed candle dict if this tick finalized a candle period, else None.
        """
        candle_slot = int(timestamp // self.timeframe) * self.timeframe
        
        if self.current_candle is None:
            self.current_candle = {
                "time": candle_slot,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": volume,
                "trades": 1
            }
            return None

        if candle_slot > self.current_candle["time"]:
            # Candle completed
            closed = dict(self.current_candle)
            self.closed_candles.append(closed)
            
            # Start new candle
            self.current_candle = {
                "time": candle_slot,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": volume,
                "trades": 1
            }
            return closed
        else:
            # Update current candle
            c = self.current_candle
            if price > c["high"]:
                c["high"] = price
            if price < c["low"]:
                c["low"] = price
            c["close"] = price
            c["volume"] += volume
            c["trades"] += 1
            return None


class VolumeContactDetector:
    """
    Detects institutional 'Volume Contact' events at critical support/resistance reference levels.
    
    A Volume Contact occurs when:
    1. Candle's high, low, or close tests a key reference level (e.g. VWAP, EMA, ORB boundary)
       within a proximity threshold (default: 0.10%).
    2. Volume on the testing bar surges significantly above its baseline (default: >= 1.8x).
    3. Rejection / Absorption characterization:
       - Bullish contact (CE): Low tests level, candle closes higher with a bottom wick rejection.
       - Bearish contact (PE): High tests level, candle closes lower with a top wick rejection.
    """
    def __init__(self, baseline_period: int = 15, contact_multiplier: float = 1.8, proximity_pct: float = 0.0010):
        self.baseline_period = baseline_period
        self.contact_multiplier = contact_multiplier
        self.proximity_pct = proximity_pct
        self.volume_history: deque = deque(maxlen=baseline_period)

    def update_volume(self, volume: float):
        if volume > 0:
            self.volume_history.append(volume)

    def check_contact(
        self,
        candle: Dict[str, Any],
        level: float,
        level_name: str = "LEVEL",
        direction: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Evaluates whether the given candle produced a Volume Contact at `level`.
        """
        open_p = candle.get("open", 0.0)
        high_p = candle.get("high", 0.0)
        low_p = candle.get("low", 0.0)
        close_p = candle.get("close", 0.0)
        vol = float(candle.get("volume", 0.0))

        if level <= 0 or close_p <= 0:
            return {
                "is_contact": False,
                "tested_level": False,
                "level_name": level_name,
                "level_price": level,
                "vol_ratio": 1.0,
                "rejection_wick": 0.0,
                "is_rejection": False,
                "distance_pct": 0.0,
                "boost_score": 0
            }

        # Calculate volume ratio against prior history
        vols = list(self.volume_history)
        if vols:
            avg_vol = sum(vols) / len(vols)
            vol_ratio = vol / avg_vol if avg_vol > 0 else 1.0
        else:
            vol_ratio = 1.25

        # Check proximity / level touch
        tolerance = level * self.proximity_pct
        tested_level = False
        is_rejection = False
        wick_ratio = 0.0
        min_dist = min(abs(low_p - level), abs(high_p - level), abs(close_p - level))
        dist_pct = min_dist / level * 100.0

        if direction == "CE":
            # Testing support from above (low reaches level zone)
            tested_level = (low_p <= level + tolerance) and (close_p >= level - tolerance)
            wick_ratio = calculate_bottom_wick_ratio(open_p, high_p, low_p, close_p)
            is_rejection = (wick_ratio >= 0.35) and (close_p >= open_p)
        elif direction == "PE":
            # Testing resistance from below (high reaches level zone)
            tested_level = (high_p >= level - tolerance) and (close_p <= level + tolerance)
            wick_ratio = calculate_top_wick_ratio(open_p, high_p, low_p, close_p)
            is_rejection = (wick_ratio >= 0.35) and (close_p <= open_p)
        else:
            # Generic touch test
            tested_level = (low_p <= level + tolerance) and (high_p >= level - tolerance)
            wick_ratio = max(
                calculate_bottom_wick_ratio(open_p, high_p, low_p, close_p),
                calculate_top_wick_ratio(open_p, high_p, low_p, close_p)
            )
            is_rejection = (wick_ratio >= 0.35)

        is_volume_spike = (vol_ratio >= self.contact_multiplier)
        is_contact = tested_level and is_volume_spike

        # Compute boost score (0 to 15)
        boost_score = 0
        if is_contact:
            boost_score += 5
            if is_rejection:
                boost_score += 5
            if vol_ratio >= 2.5:
                boost_score += 5

        # Record this candle volume into history after check
        self.update_volume(vol)

        return {
            "is_contact": is_contact,
            "tested_level": tested_level,
            "level_name": level_name,
            "level_price": round(level, 2),
            "vol_ratio": round(vol_ratio, 2),
            "rejection_wick": round(wick_ratio, 3),
            "is_rejection": is_rejection,
            "distance_pct": round(dist_pct, 3),
            "boost_score": boost_score
        }


class VolumeDryUpDetector:
    """
    Detects 'Volume Dry Up' (VDU) patterns indicating seller/buyer exhaustion before explosive breakouts.

    Pattern characteristics:
    1. Dry-up / Contraction phase:
       - Either N consecutive bars of decreasing volume (default: >= 3 bars), OR
       - Volume running significantly below average (< 0.70x rolling avg) for 2+ bars,
         accompanied by contracting candle spread (volatility compression).
    2. Ignition / Expansion phase:
       - The candle immediately following the dry-up phase breaks out with volume surging:
         candle_vol >= ignition_multiplier * avg_dry_volume (default: >= 2.0x).
       - Candle range expands significantly compared to dry-up range.
    """
    def __init__(
        self,
        lookback_period: int = 15,
        min_dry_bars: int = 3,
        dry_vol_ratio: float = 0.70,
        ignition_multiplier: float = 2.0
    ):
        self.lookback_period = lookback_period
        self.min_dry_bars = min_dry_bars
        self.dry_vol_ratio = dry_vol_ratio
        self.ignition_multiplier = ignition_multiplier

        self.candle_history: deque = deque(maxlen=lookback_period)
        self.dry_streak = 0
        self.dry_volumes: List[float] = []
        self.dry_ranges: List[float] = []

    def update(self, candle: Dict[str, Any]) -> Dict[str, Any]:
        """
        Processes a newly closed candle and returns current dry-up and ignition state.
        """
        vol = float(candle.get("volume", 0.0))
        high_p = float(candle.get("high", 0.0))
        low_p = float(candle.get("low", 0.0))
        candle_range = max(0.1, high_p - low_p)

        history = list(self.candle_history)
        avg_vol = (sum(c["volume"] for c in history) / len(history)) if history else vol
        avg_range = (sum(c["high"] - c["low"] for c in history) / len(history)) if history else candle_range

        # Dry candle criteria:
        # 1. Volume strictly less than previous bar
        cond_decreasing = (len(history) >= 1 and vol < history[-1]["volume"])
        # 2. Volume below dry_vol_ratio of average
        cond_low_vol = (vol <= avg_vol * self.dry_vol_ratio)
        # 3. Range compression
        cond_compression = (candle_range <= avg_range * 0.90)

        is_dry_bar = (cond_decreasing or cond_low_vol) and (candle_range <= avg_range * 1.2)

        is_ignition = False
        ign_ratio = 1.0
        rng_expansion = 1.0
        boost_score = 0
        prior_dry_bars = self.dry_streak

        if is_dry_bar:
            self.dry_streak += 1
            self.dry_volumes.append(vol)
            self.dry_ranges.append(candle_range)
        else:
            # Not a dry bar. Did we just emerge from a dry-up phase?
            if prior_dry_bars >= self.min_dry_bars:
                avg_dry_vol = (sum(self.dry_volumes) / len(self.dry_volumes)) if self.dry_volumes else avg_vol
                avg_dry_rng = (sum(self.dry_ranges) / len(self.dry_ranges)) if self.dry_ranges else avg_range

                ign_ratio = vol / (avg_dry_vol if avg_dry_vol > 0 else 1.0)
                rng_expansion = candle_range / (avg_dry_rng if avg_dry_rng > 0 else 1.0)

                if ign_ratio >= self.ignition_multiplier:
                    is_ignition = True
                    # Boost score up to 15 points
                    boost_score = min(15, int(8 + (ign_ratio - self.ignition_multiplier) * 4))

            # Reset dry streak
            self.dry_streak = 0
            self.dry_volumes.clear()
            self.dry_ranges.clear()

        # Update history
        self.candle_history.append(candle)

        return {
            "is_dry_up": self.dry_streak >= self.min_dry_bars,
            "dry_bars": self.dry_streak if is_dry_bar else prior_dry_bars,
            "is_ignition": is_ignition,
            "ignition_ratio": round(ign_ratio, 2),
            "range_expansion": round(rng_expansion, 2),
            "boost_score": boost_score
        }

