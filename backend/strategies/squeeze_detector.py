import math
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from collections import deque
from loguru import logger
from backend.strategies.base_strategy import BaseStrategy
from backend.strategies.indicators import IncrementalEMA, IncrementalATR, CandleAggregator

class SqueezeDetector(BaseStrategy):
    """
    Bollinger Bands / Keltner Channel Volatility Squeeze Detector.
    Detects the severe volatility compression and coiling that always precedes
    violent multi-standard-deviation explosive impulse spikes.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(name="SQUEEZE_DETECTOR")
        cfg = config or {}
        self.start_time = cfg.get("start_time", "09:20:00")
        self.end_time = cfg.get("end_time", "15:20:00")
        self.period = cfg.get("period", 20)
        self.bb_mult = cfg.get("bb_multiplier", 2.0)
        self.kc_mult = cfg.get("kc_multiplier", 1.5)

        # 3-min candle aggregators
        self.aggregators: Dict[str, CandleAggregator] = {
            "NIFTY": CandleAggregator(timeframe_seconds=180),
            "SENSEX": CandleAggregator(timeframe_seconds=180)
        }

        # Spot indicators per instrument
        self.candles: Dict[str, deque] = {
            "NIFTY": deque(maxlen=30),
            "SENSEX": deque(maxlen=30)
        }
        self.ema20: Dict[str, IncrementalEMA] = {
            "NIFTY": IncrementalEMA(period=20),
            "SENSEX": IncrementalEMA(period=20)
        }
        self.atr: Dict[str, IncrementalATR] = {
            "NIFTY": IncrementalATR(period=20),
            "SENSEX": IncrementalATR(period=20)
        }

        self.spot_prices: Dict[str, float] = {"NIFTY": 0.0, "SENSEX": 0.0}
        self.squeeze_bars: Dict[str, int] = {"NIFTY": 0, "SENSEX": 0}
        self.is_in_squeeze: Dict[str, bool] = {"NIFTY": False, "SENSEX": False}

    def update_spot(self, inst: str, price: float, ts: float, vol: float = 1.0):
        self.spot_prices[inst] = price
        if inst in self.aggregators:
            closed = self.aggregators[inst].on_tick(ts, price, vol)
            if closed:
                self._on_candle_close(inst, closed, ts)

    def seed_from_spot(self, inst: str, spot_info: Dict[str, Any]):
        ltp = float(spot_info.get("ltp", 0.0))
        if ltp > 0:
            self.spot_prices[inst] = ltp
            if inst in self.ema20:
                self.ema20[inst].seed(ltp)

    def seed_from_candles(self, inst: str, candles: List[Dict[str, Any]]):
        if not candles:
            return
        logger.info(f"🔄 [SQUEEZE_DETECTOR] Warming up {inst} with {len(candles)} historical candles...")
        for c in candles:
            cl = float(c.get("close", 0.0))
            hi = float(c.get("high", cl))
            lo = float(c.get("low", cl))
            if cl <= 0:
                continue
            self.spot_prices[inst] = cl
            self.candles[inst].append(cl)
            if inst in self.ema20:
                self.ema20[inst].update(cl)
            if inst in self.atr:
                self.atr[inst].update(hi, lo, cl)

        # Evaluate initial squeeze status
        if len(self.candles[inst]) >= self.period:
            self._evaluate_squeeze(inst, self.candles[inst][-1], datetime.now(timezone.utc).timestamp())

    def _on_candle_close(self, inst: str, candle: Dict[str, Any], ts: float):
        cl = candle["close"]
        hi = candle["high"]
        lo = candle["low"]

        self.candles[inst].append(cl)
        self.ema20[inst].update(cl)
        self.atr[inst].update(hi, lo, cl)

        if len(self.candles[inst]) >= self.period:
            self._evaluate_squeeze(inst, cl, ts)

    def _evaluate_squeeze(self, inst: str, spot: float, ts: float):
        closes = list(self.candles[inst])[-self.period:]
        mean_p = sum(closes) / len(closes)
        variance = sum((p - mean_p) ** 2 for p in closes) / len(closes)
        std_dev = math.sqrt(variance)

        # Bollinger Bands
        upper_bb = mean_p + self.bb_mult * std_dev
        lower_bb = mean_p - self.bb_mult * std_dev

        # Keltner Channels
        atr_val = self.atr[inst].value
        ema_val = self.ema20[inst].value or mean_p
        upper_kc = ema_val + self.kc_mult * atr_val
        lower_kc = ema_val - self.kc_mult * atr_val

        # Squeeze condition: BB is entirely inside KC
        squeeze_active = (upper_bb < upper_kc) and (lower_bb > lower_kc)

        if squeeze_active:
            self.squeeze_bars[inst] += 1
            self.is_in_squeeze[inst] = True
            bars = self.squeeze_bars[inst]
            if bars >= 4 and bars % 3 == 0:
                self.emit_radar_alert(
                    alert_type="VOLATILITY_COIL_ACTIVE",
                    instrument=inst,
                    direction="CE" if spot > ema_val else "PE",
                    title=f"⏳ {inst} Volatility Squeeze Coiling ({bars} bars)",
                    message=f"{inst} Bollinger Bands compressed inside Keltner Channels for {bars * 3} mins (ATR: {atr_val:.1f}). Extreme compression precedes multi-sigma impulse explosion.",
                    meta_details={"spot": spot, "squeeze_bars": bars, "atr": round(atr_val, 2)},
                    now_ts=ts
                )
        else:
            # Check for Squeeze Release (Squeeze Firing)
            if self.is_in_squeeze.get(inst, False):
                self.is_in_squeeze[inst] = False
                bars = self.squeeze_bars[inst]
                self.squeeze_bars[inst] = 0

                direction = "CE" if spot > ema_val else "PE"
                self.emit_radar_alert(
                    alert_type="SQUEEZE_BREAKOUT_FIRING",
                    instrument=inst,
                    direction=direction,
                    title=f"💥 {inst} SQUEEZE BREAKOUT FIRING ({direction})!",
                    message=f"{inst} Squeeze fired after {bars * 3} mins of compression! Spot {spot:.1f} released {'above' if direction=='CE' else 'below'} 20 EMA ({ema_val:.1f}). High-velocity expansion initiated.",
                    meta_details={"spot": spot, "squeeze_bars": bars, "ema20": round(ema_val, 2)},
                    now_ts=ts
                )

    async def on_tick(self, tick: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        ltp = float(tick.get("ltp", 0.0))
        vol = float(tick.get("volume", 1.0))
        raw_ts = tick.get("exchange_timestamp") or datetime.now(timezone.utc).timestamp()
        ts = float(raw_ts) if raw_ts > 1e11 else float(raw_ts)
        if ts > 1e11:
            ts /= 1000.0

        if meta and meta.get("is_spot"):
            inst = meta.get("name", "NIFTY")
            self.update_spot(inst, ltp, ts, vol)
            return None

        return None
