"""
PROJECT ALPHA 3.0 — FLOW ENGINE v2 (Estimated Order Flow)

Estimated order-flow engine using AngelOne SmartAPI FULL/SnapQuote data (LTP, LTQ, volume,
OI, total buy/sell resting qty, and best-5 bid/ask depth).

HONESTY CONTRACT (baked into the design, per hard lessons this session):
  - AngelOne does NOT provide true aggressor-side trade data. Trade direction here is
    ESTIMATED via quote-test (P>=ask -> buy, P<=bid -> sell) with a tick-test fallback.
    Every classification records HOW it was decided ("ASK"/"BID"/"TICK"/"CARRY").
  - total_buy_qty / total_sell_qty are RESTING order-book quantities (liquidity context),
    NEVER used as executed buy/sell volume.
  - Weights are an engineering STARTING config, NOT statistically validated. Configurable.
  - FlowIndex must NEVER directly trigger an order. It is advisory context + a confluence
    input, gated by a confidence score, and only after backtest validation.
  - Pluggable: the ONLY source of trade direction is classify_trade(); swap in a true
    aggressor feed later without touching the rest of the engine.

Ships DISABLED. Requires: (1) live-verified depth decode in binary_parser, (2) fresh
recorded data WITH depth, (3) backtest validation — before any trust or wiring.
"""
import math
from collections import deque
from datetime import datetime, timezone, time
from typing import Dict, Any, Optional, List, Tuple, Deque
from loguru import logger
from backend.strategies.base_strategy import BaseStrategy


# ----------------------------------------------------------------------------
# Rolling z-score helper (deterministic, unit-testable)
# ----------------------------------------------------------------------------
class RollingZ:
    """Rolling mean/std with clip+normalize to [-1, 1]. Returns 0 until warmed up."""
    def __init__(self, window: int = 200, min_samples: int = 30, clip: float = 3.0):
        self.window = int(window)
        self.min_samples = int(min_samples)
        self.clip = float(clip)
        self._buf: Deque[float] = deque(maxlen=self.window)

    def update(self, x: float) -> float:
        self._buf.append(float(x))
        return self.normalized(x)

    def warmed(self) -> bool:
        return len(self._buf) >= self.min_samples

    def normalized(self, x: float) -> float:
        if len(self._buf) < self.min_samples:
            return 0.0
        n = len(self._buf)
        mean = sum(self._buf) / n
        var = sum((v - mean) ** 2 for v in self._buf) / n
        std = math.sqrt(var)
        if std <= 1e-9:
            return 0.0
        z = (x - mean) / std
        z = max(-self.clip, min(self.clip, z))
        return z / self.clip


# ----------------------------------------------------------------------------
# Trade classifier (THE pluggable direction source)
# ----------------------------------------------------------------------------
def classify_trade(ltp: float, best_bid: Optional[float], best_ask: Optional[float],
                   prev_ltp: Optional[float], prev_side: int) -> Tuple[int, str]:
    """
    Returns (side, method). side: +1 buy-initiated, -1 sell-initiated, 0 unknown.
    Quote test first (needs valid depth), then tick test, then carry.
    Swap this function for a true aggressor feed later; the engine interface is unchanged.
    """
    if best_ask is not None and best_ask > 0 and ltp >= best_ask:
        return +1, "ASK"
    if best_bid is not None and best_bid > 0 and ltp <= best_bid:
        return -1, "BID"
    # inside spread (or no depth) -> tick test
    if prev_ltp is not None:
        if ltp > prev_ltp:
            return +1, "TICK"
        if ltp < prev_ltp:
            return -1, "TICK"
        return (prev_side if prev_side != 0 else 0), "CARRY"
    return 0, "NONE"


class FlowEngineV2(BaseStrategy):
    def __init__(self, config: Optional[Dict[str, Any]] = None, regime_filter: Optional[Any] = None):
        super().__init__(name="FLOW_V2")
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", False))  # ships disabled until validated
        self.regime_filter = regime_filter
        self.start_time = cfg.get("start_time", "09:20:00")
        self.end_time = cfg.get("end_time", "15:25:00")

        self.window_sec = float(cfg.get("window_sec", 60.0))       # rolling flow window
        self.z_window = int(cfg.get("z_window", 200))
        self.z_min_samples = int(cfg.get("z_min_samples", 30))
        self.depth_weights = cfg.get("depth_weights", [1.0, 0.75, 0.50, 0.30, 0.15])
        self.eps = 1e-9

        # Composite weights (configurable, NOT validated).
        w = cfg.get("weights", {})
        self.w_delta = float(w.get("delta", 0.30))
        self.w_delta_vel = float(w.get("delta_velocity", 0.20))
        self.w_pressure = float(w.get("pressure", 0.15))
        self.w_depth = float(w.get("depth", 0.10))
        self.w_oi = float(w.get("oi", 0.10))
        self.w_volume = float(w.get("volume", 0.10))
        self.w_efficiency = float(w.get("efficiency", 0.05))

        # Absorption thresholds.
        self.absorption_delta_z = float(cfg.get("absorption_delta_z", 0.5))
        self.absorption_price_z = float(cfg.get("absorption_price_z", 0.3))
        self.absorption_min_persist = int(cfg.get("absorption_min_persist", 2))

        # Per-token flow state.
        self._state: Dict[str, Dict[str, Any]] = {}

        # Normalizers per token+metric.
        self._z: Dict[str, Dict[str, RollingZ]] = {}

        # Latest output per token (queryable).
        self._latest: Dict[str, Dict[str, Any]] = {}

    # -------- lifecycle / seeding --------
    def seed_from_spot(self, inst: str, spot_info: Dict[str, Any]):
        return

    def seed_from_candles(self, inst: str, candles: List[Dict[str, Any]]):
        return

    def get_flow(self, token: str) -> Optional[Dict[str, Any]]:
        """Public API: latest flow output for a token (for confluence use later)."""
        return self._latest.get(token)

    def _in_window(self, dt: datetime) -> bool:
        t = dt.time()
        sh, sm, ss = map(int, self.start_time.split(":"))
        eh, em, es = map(int, self.end_time.split(":"))
        return time(sh, sm, ss) <= t <= time(eh, em, es)

    def _z_for(self, token: str, metric: str) -> RollingZ:
        d = self._z.setdefault(token, {})
        if metric not in d:
            d[metric] = RollingZ(self.z_window, self.z_min_samples)
        return d[metric]

    @staticmethod
    def weighted_depth_imbalance(bids: List[Dict[str, Any]], asks: List[Dict[str, Any]],
                                 weights: List[float], eps: float = 1e-9) -> float:
        wb = sum(weights[i] * bids[i].get("qty", 0.0) for i in range(min(len(bids), len(weights))))
        wa = sum(weights[i] * asks[i].get("qty", 0.0) for i in range(min(len(asks), len(weights))))
        denom = wb + wa
        if denom <= eps:
            return 0.0
        return (wb - wa) / denom

    @staticmethod
    def oi_regime(price_change: float, oi_change: float) -> str:
        if price_change > 0 and oi_change > 0:
            return "LONG_BUILDUP"
        if price_change < 0 and oi_change > 0:
            return "SHORT_BUILDUP"
        if price_change > 0 and oi_change < 0:
            return "SHORT_COVERING"
        if price_change < 0 and oi_change < 0:
            return "LONG_UNWINDING"
        return "NONE"

    # -------- tick handler --------
    async def on_tick(self, tick: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        # This engine analyzes TRADED instruments (options). Spot has no traded volume.
        if not meta or meta.get("is_spot") or not meta.get("option_type"):
            return None

        raw_ts = tick.get("exchange_timestamp") or datetime.now(timezone.utc).timestamp()
        ts = float(raw_ts)
        if ts > 1e11:
            ts /= 1000.0
        now_dt = self.parse_ist_time(ts)
        if not self._in_window(now_dt):
            return None

        ltp = float(tick.get("ltp", 0.0))
        ltq = float(tick.get("last_traded_qty", 0.0))
        # ---- bad-tick rejection (safety, per spec §16) ----
        if ltp <= 0 or ltq < 0:
            return None

        token = str(tick.get("token", ""))
        best_bid = tick.get("best_bid")
        best_ask = tick.get("best_ask")
        oi = float(tick.get("open_interest", 0.0))
        vol = float(tick.get("volume", 0.0))

        st = self._state.setdefault(token, {
            "prev_ltp": None, "prev_side": 0, "cvd": 0.0,
            "buy_win": deque(), "sell_win": deque(),  # (ts, qty)
            "price_win": deque(), "vol_win": deque(),  # (ts, ltp)/(ts, vol)
            "oi_win": deque(),
            "cvd_hist": deque(),  # (ts, cvd)
            "prev_delta_velocity": 0.0,
            "absorption_streak": 0,
        })

        # ---- trade direction (estimated) ----
        side, method = classify_trade(ltp, best_bid, best_ask, st["prev_ltp"], st["prev_side"])
        st["prev_ltp"] = ltp
        if side != 0:
            st["prev_side"] = side

        buy_vol = ltq if side > 0 else 0.0
        sell_vol = ltq if side < 0 else 0.0
        delta = buy_vol - sell_vol
        st["cvd"] += delta

        # rolling windows
        for key, val in (("buy_win", buy_vol), ("sell_win", sell_vol)):
            st[key].append((ts, val))
        st["price_win"].append((ts, ltp))
        st["vol_win"].append((ts, vol))
        st["oi_win"].append((ts, oi))
        st["cvd_hist"].append((ts, st["cvd"]))
        for key in ("buy_win", "sell_win", "price_win", "vol_win", "oi_win", "cvd_hist"):
            w = st[key]
            while w and (ts - w[0][0]) > self.window_sec:
                w.popleft()

        # ---- components ----
        buy_w = sum(q for _, q in st["buy_win"])
        sell_w = sum(q for _, q in st["sell_win"])
        pressure = (buy_w - sell_w) / max(buy_w + sell_w, self.eps)

        # delta velocity: CVD change over window / elapsed
        cvd_old = st["cvd_hist"][0][1] if st["cvd_hist"] else st["cvd"]
        elapsed = max(ts - st["cvd_hist"][0][0], 1.0) if st["cvd_hist"] else 1.0
        delta_velocity = (st["cvd"] - cvd_old) / elapsed
        delta_acceleration = delta_velocity - st["prev_delta_velocity"]
        st["prev_delta_velocity"] = delta_velocity

        # depth imbalance (resting liquidity context only)
        depth = tick.get("depth") or {}
        wdi = self.weighted_depth_imbalance(depth.get("bids", []), depth.get("asks", []),
                                            self.depth_weights, self.eps)

        # OI change over window
        oi_old = st["oi_win"][0][1] if st["oi_win"] else oi
        oi_change = oi - oi_old

        # price change + volume over window
        price_old = st["price_win"][0][1] if st["price_win"] else ltp
        price_change = ltp - price_old
        vol_old = st["vol_win"][0][1] if st["vol_win"] else vol
        vol_change = vol - vol_old
        volume_velocity = vol_change / elapsed
        price_efficiency = abs(price_change) / max(vol_change, self.eps)

        # ---- normalization (rolling z -> [-1,1]) ----
        n_delta = self._z_for(token, "delta").update(delta)
        n_delta_vel = self._z_for(token, "delta_vel").update(delta_velocity)
        n_pressure = max(-1.0, min(1.0, pressure))          # already bounded
        n_depth = max(-1.0, min(1.0, wdi))                  # already bounded
        n_oi = self._z_for(token, "oi").update(oi_change)
        n_vol = self._z_for(token, "vol").update(volume_velocity)
        n_eff = self._z_for(token, "eff").update(price_efficiency)

        # ---- composite flow score -> FlowIndex ----
        flow_score = (self.w_delta * n_delta + self.w_delta_vel * n_delta_vel
                      + self.w_pressure * n_pressure + self.w_depth * n_depth
                      + self.w_oi * n_oi + self.w_volume * n_vol
                      + self.w_efficiency * n_eff)
        flow_index = 100.0 * math.tanh(flow_score)

        # ---- absorption (needs persistence) ----
        n_price = self._z_for(token, "price_abs").update(abs(price_change))
        absorption = "NONE"
        strong_delta = abs(n_delta) >= self.absorption_delta_z
        weak_price = abs(n_price) <= self.absorption_price_z
        if strong_delta and weak_price:
            st["absorption_streak"] += 1
            if st["absorption_streak"] >= self.absorption_min_persist:
                absorption = "BUY" if n_delta > 0 else "SELL"
        else:
            st["absorption_streak"] = 0

        # ---- OI regime + confidence ----
        regime = self.oi_regime(price_change, oi_change)
        price_dir = 1 if price_change > 0 else (-1 if price_change < 0 else 0)
        cvd_dir = 1 if delta_velocity > 0 else (-1 if delta_velocity < 0 else 0)
        delta_dir = 1 if delta > 0 else (-1 if delta < 0 else 0)
        depth_dir = 1 if wdi > 0.1 else (-1 if wdi < -0.1 else 0)
        oi_dir = 1 if oi_change > 0 else (-1 if oi_change < 0 else 0)
        target = 1 if flow_index > 0 else (-1 if flow_index < 0 else 0)
        c1 = 1 if delta_dir == price_dir and price_dir != 0 else 0
        c2 = 1 if cvd_dir == price_dir and price_dir != 0 else 0
        c3 = 1 if depth_dir == target and target != 0 else 0
        c4 = 1 if oi_dir != 0 else 0
        c5 = 1 if self._z_for(token, "vol").warmed() and n_vol > 0.2 else 0
        confidence = (c1 + c2 + c3 + c4 + c5) / 5.0 * 100.0

        out = {
            "token": token, "instrument": meta.get("name"), "strike": meta.get("strike"),
            "option_type": meta.get("option_type"), "timestamp": ts, "price": ltp,
            "trade_side": "BUY" if side > 0 else ("SELL" if side < 0 else "NONE"),
            "classification_method": method,
            "ltq": ltq, "buy_volume": buy_vol, "sell_volume": sell_vol,
            "delta": delta, "cvd": st["cvd"], "pressure": round(pressure, 3),
            "delta_velocity": round(delta_velocity, 3), "delta_acceleration": round(delta_acceleration, 3),
            "depth_imbalance": round(wdi, 3), "oi": oi, "oi_change": oi_change,
            "oi_regime": regime, "volume_velocity": round(volume_velocity, 3),
            "price_efficiency": price_efficiency, "absorption": absorption,
            "flow_score": round(flow_score, 4), "flow_index": round(flow_index, 1),
            "confidence": round(confidence, 0),
        }
        self._latest[token] = out
        return None  # advisory context only; never emits a trade signal directly
