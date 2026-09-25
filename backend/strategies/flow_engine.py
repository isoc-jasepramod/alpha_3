from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from collections import deque
from loguru import logger
from backend.strategies.base_strategy import BaseStrategy

class FlowEngine(BaseStrategy):
    """
    Order Flow Imbalance & Cumulative Volume Delta (CVD) Engine.
    Uses real-time total_buy_qty vs total_sell_qty and tick-level volume delta.
    Detects aggressive institutional absorption at consolidation boundaries before breakouts.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(name="FLOW_ENGINE")
        cfg = config or {}
        self.start_time = cfg.get("start_time", "09:15:00")
        self.end_time = cfg.get("end_time", "15:25:00")
        self.absorption_window_sec = cfg.get("absorption_window_sec", 180) # 3 min
        self.min_cvd_surge = cfg.get("min_cvd_surge", 50000) # Contracts

        # Spot prices
        self.spot_prices: Dict[str, float] = {"NIFTY": 0.0, "SENSEX": 0.0}

        # Tick tracking per token: token -> (last_ltp, last_vol, cvd)
        self.token_flow: Dict[str, Dict[str, Any]] = {}

        # Rolling history per instrument: deque of (ts, spot, cvd_sum, queue_ratio)
        self.flow_history: Dict[str, deque] = {
            "NIFTY": deque(maxlen=120),
            "SENSEX": deque(maxlen=120)
        }

    def update_spot(self, inst: str, price: float, ts: float):
        if price > 0:
            self.spot_prices[inst] = price

    def seed_from_spot(self, inst: str, spot_info: Dict[str, Any]):
        ltp = float(spot_info.get("ltp", 0.0))
        if ltp > 0:
            self.spot_prices[inst] = ltp

    def seed_from_candles(self, inst: str, candles: List[Dict[str, Any]]):
        if candles:
            latest = candles[-1]
            cl = float(latest.get("close", 0.0))
            if cl > 0:
                self.spot_prices[inst] = cl

    async def on_tick(self, tick: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        token = str(tick.get("token", ""))
        ltp = float(tick.get("ltp", 0.0))
        vol = float(tick.get("volume", 0.0))
        total_buy = float(tick.get("total_buy_qty", 0.0))
        total_sell = float(tick.get("total_sell_qty", 0.0))
        raw_ts = tick.get("exchange_timestamp") or datetime.now(timezone.utc).timestamp()
        ts = float(raw_ts) if raw_ts > 1e11 else float(raw_ts)
        if ts > 1e11:
            ts /= 1000.0

        if meta and meta.get("is_spot"):
            inst = meta.get("name", "NIFTY")
            self.update_spot(inst, ltp, ts)
            return None

        if not meta or ltp <= 0:
            return None

        inst = meta.get("name", "NIFTY")
        spot = self.spot_prices.get(inst, 0.0)
        if spot <= 0:
            return None

        now_dt = self.parse_ist_time(ts)
        if not self.is_time_gated(now_dt, self.start_time, self.end_time):
            return None

        # Track tick-level volume delta
        prev = self.token_flow.get(token)
        if not prev:
            self.token_flow[token] = {
                "ltp": ltp,
                "vol": vol,
                "cvd": 0.0,
                "ts": ts
            }
            return None

        delta_vol = max(0.0, vol - prev["vol"])
        prev_ltp = prev["ltp"]
        cur_cvd = prev["cvd"]

        if delta_vol > 0:
            if ltp > prev_ltp:
                cur_cvd += delta_vol
            elif ltp < prev_ltp:
                cur_cvd -= delta_vol
            else:
                # Same price: allocate delta based on queue imbalance
                total_q = total_buy + total_sell
                if total_q > 0:
                    weight = (total_buy - total_sell) / total_q
                    cur_cvd += delta_vol * weight

        self.token_flow[token] = {
            "ltp": ltp,
            "vol": vol,
            "cvd": cur_cvd,
            "ts": ts
        }

        # Track aggregate flow on ATM and near ATM options
        offset = meta.get("offset")
        if offset in (-1, 0, 1):
            total_active_cvd = sum(f["cvd"] for f in self.token_flow.values() if (ts - f["ts"]) < 60.0)
            queue_ratio = (total_buy / (total_sell + 1e-4)) if total_sell > 0 else 1.0

            hist = self.flow_history[inst]
            hist.append((ts, spot, total_active_cvd, queue_ratio))
            while hist and (ts - hist[0][0]) > self.absorption_window_sec:
                hist.popleft()

            if len(hist) >= 10 and (ts - hist[0][0]) >= 90.0:
                self._check_absorption(inst, ts, spot, hist)

        return None

    def _check_absorption(self, inst: str, ts: float, spot: float, hist: deque):
        old_ts, old_spot, old_cvd, _ = hist[0]
        cur_ts, cur_spot, cur_cvd, cur_q_ratio = hist[-1]

        spot_change_pct = abs(cur_spot - old_spot) / old_spot * 100.0
        delta_cvd = cur_cvd - old_cvd

        # PASSIVE ABSORPTION DETECTION:
        # Spot is flat (<0.10% move) while CVD is surging heavily
        if spot_change_pct <= 0.12:
            # Bullish Absorption: CVD surging up (aggressive buyers absorbing limit sells)
            if delta_cvd > self.min_cvd_surge and cur_q_ratio > 1.4:
                alert_key = f"FLOW_ABSORP_CE_{inst}"
                self.emit_radar_alert(
                    alert_type="ORDER_FLOW_ABSORPTION_CE",
                    instrument=inst,
                    direction="CE",
                    title=f"🌊 {inst} Aggressive Order Flow Absorption (CE Precursor)",
                    message=f"{inst} spot flat at {spot:.1f}, but aggressive Buyer CVD surged +{int(delta_cvd):,} contracts (Bid/Ask Queue: {cur_q_ratio:.2f}x). Limit sell walls being exhausted.",
                    meta_details={"spot": spot, "delta_cvd": int(delta_cvd), "queue_ratio": round(cur_q_ratio, 2)},
                    now_ts=ts
                )
            # Bearish Absorption: CVD dumping down (aggressive sellers absorbing limit bids)
            elif delta_cvd < -self.min_cvd_surge and cur_q_ratio < 0.7:
                alert_key = f"FLOW_ABSORP_PE_{inst}"
                self.emit_radar_alert(
                    alert_type="ORDER_FLOW_ABSORPTION_PE",
                    instrument=inst,
                    direction="PE",
                    title=f"🔻 {inst} Aggressive Order Flow Absorption (PE Precursor)",
                    message=f"{inst} spot pinned at {spot:.1f}, but aggressive Seller CVD dumped {int(delta_cvd):,} contracts (Bid/Ask Queue: {cur_q_ratio:.2f}x). Passive bid liquidity vanishing.",
                    meta_details={"spot": spot, "delta_cvd": int(delta_cvd), "queue_ratio": round(cur_q_ratio, 2)},
                    now_ts=ts
                )
