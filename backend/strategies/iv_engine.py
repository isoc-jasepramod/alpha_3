import math
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple, List
from collections import deque
from loguru import logger
from backend.strategies.base_strategy import BaseStrategy

# Fast normal CDF approximation (Abramowitz and Stegun)
def norm_cdf(x: float) -> float:
    k = 1.0 / (1.0 + 0.2316419 * abs(x))
    k_sum = k * (0.319381530 + k * (-0.356563782 + k * (1.781477937 + k * (-1.821255978 + 1.330274429 * k))))
    w = 1.0 - 0.3989422804014327 * math.exp(-0.5 * x * x) * k_sum
    return w if x >= 0.0 else 1.0 - w

def norm_pdf(x: float) -> float:
    return 0.3989422804014327 * math.exp(-0.5 * x * x)

def bs_price_and_greeks(
    spot: float,
    strike: float,
    t_years: float,
    sigma: float,
    r: float = 0.07,
    option_type: str = "CE"
) -> Tuple[float, float, float]:
    """
    Returns (price, delta, gamma)
    """
    if spot <= 0 or strike <= 0 or t_years <= 0 or sigma <= 0:
        return 0.0, 0.0, 0.0

    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    df = math.exp(-r * t_years)

    pdf_d1 = norm_pdf(d1)
    gamma = pdf_d1 / (spot * sigma * sqrt_t)

    if option_type == "CE":
        price = spot * norm_cdf(d1) - strike * df * norm_cdf(d2)
        delta = norm_cdf(d1)
    else:
        price = strike * df * norm_cdf(-d2) - spot * norm_cdf(-d1)
        delta = norm_cdf(d1) - 1.0

    return price, delta, gamma

def solve_iv(
    market_price: float,
    spot: float,
    strike: float,
    t_years: float,
    r: float = 0.07,
    option_type: str = "CE",
    max_iter: int = 15,
    tol: float = 1e-3
) -> Optional[float]:
    """
    Solves for Implied Volatility using Newton-Raphson with bisection fallback.
    """
    if market_price <= 0 or spot <= 0 or strike <= 0 or t_years <= 0:
        return None

    # Intrinsic value check
    intrinsic = max(0.0, spot - strike) if option_type == "CE" else max(0.0, strike - spot)
    if market_price < intrinsic:
        return None

    sigma = 0.20  # Initial guess 20%
    sqrt_t = math.sqrt(t_years)

    for _ in range(max_iter):
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t_years) / (sigma * sqrt_t)
        d2 = d1 - sigma * sqrt_t
        df = math.exp(-r * t_years)

        if option_type == "CE":
            theo_price = spot * norm_cdf(d1) - strike * df * norm_cdf(d2)
        else:
            theo_price = strike * df * norm_cdf(-d2) - spot * norm_cdf(-d1)

        diff = theo_price - market_price
        if abs(diff) < tol:
            return sigma

        vega = spot * sqrt_t * norm_pdf(d1)
        if vega < 1e-4:
            break

        sigma -= diff / vega
        if sigma <= 0.01 or sigma > 3.0:
            break

    # Bisection fallback
    low, high = 0.02, 3.0
    for _ in range(12):
        mid = (low + high) / 2.0
        d1 = (math.log(spot / strike) + (r + 0.5 * mid * mid) * t_years) / (mid * sqrt_t)
        d2 = d1 - mid * sqrt_t
        df = math.exp(-r * t_years)
        theo = (spot * norm_cdf(d1) - strike * df * norm_cdf(d2)) if option_type == "CE" else (strike * df * norm_cdf(-d2) - spot * norm_cdf(-d1))
        if theo < market_price:
            low = mid
        else:
            high = mid
        if abs(theo - market_price) < 0.05:
            return mid

    return mid if 0.02 <= mid <= 3.0 else None


class IVEngine(BaseStrategy):
    """
    Predictive IV & Skew Velocity Engine.
    Monitors live implied volatility and risk reversal skew shifts in real time.
    Detects aggressive institutional positioning 5-15 minutes BEFORE spot breakouts.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(name="IV_ENGINE")
        cfg = config or {}
        self.start_time = cfg.get("start_time", "09:20:00")
        self.end_time = cfg.get("end_time", "15:25:00")
        self.r = cfg.get("risk_free_rate", 0.07)
        self.skew_velocity_threshold = cfg.get("skew_velocity_threshold", 2.2) # +2.2% IV skew expansion
        self.tau_sec = cfg.get("tau_sec", 300) # 5-min rolling window

        # Spot prices: "NIFTY", "SENSEX"
        self.spot_prices: Dict[str, float] = {"NIFTY": 0.0, "SENSEX": 0.0}

        # Strike-wise latest IV: token -> (timestamp, iv, gamma, delta, ltp, oi)
        self.token_iv: Dict[str, Tuple[float, float, float, float, float, float]] = {}

        # Rolling Risk Reversal history per instrument: deque of (ts, skew_val)
        self.skew_history: Dict[str, deque] = {
            "NIFTY": deque(maxlen=60),
            "SENSEX": deque(maxlen=60)
        }

    def _get_time_to_expiry_years(self, ts: float) -> float:
        """Approximates trading time left to weekly expiry (Thursday 15:30 IST)"""
        dt = self.parse_ist_time(ts)
        days_ahead = (3 - dt.weekday()) % 7
        target = dt.replace(hour=15, minute=30, second=0, microsecond=0) + timedelta(days=days_ahead)
        if target <= dt:
            target += timedelta(days=7)
        diff_hours = (target - dt).total_seconds() / 3600.0
        return max(diff_hours / (365.25 * 24.0), 0.0001)

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

    def get_token_greeks(self, token: str) -> Optional[Tuple[float, float, float, float]]:
        """Returns (iv, gamma, delta, ltp) for token"""
        val = self.token_iv.get(token)
        return (val[1], val[2], val[3], val[4]) if val else None

    async def on_tick(self, tick: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        token = str(tick.get("token", ""))
        ltp = float(tick.get("ltp", 0.0))
        oi = float(tick.get("open_interest", 0.0))
        raw_ts = tick.get("exchange_timestamp") or datetime.now(timezone.utc).timestamp()
        ts = float(raw_ts) if raw_ts > 1e11 else float(raw_ts)
        if ts > 1e11:
            ts /= 1000.0

        # Handle spot index ticks
        if meta and meta.get("is_spot"):
            inst = meta.get("name", "NIFTY")
            self.update_spot(inst, ltp, ts)
            return None

        if not meta or not meta.get("option_type") or ltp <= 0.5:
            return None

        inst = meta.get("name", "NIFTY")
        spot = self.spot_prices.get(inst, 0.0)
        if spot <= 0:
            return None

        strike = float(meta.get("strike", 0.0))
        opt_type = meta.get("option_type") # "CE" or "PE"
        offset = meta.get("offset") # -2, -1, 0, 1, 2
        t_years = self._get_time_to_expiry_years(ts)

        # Solve live IV
        iv = solve_iv(ltp, spot, strike, t_years, self.r, opt_type)
        if not iv:
            return None

        _, delta, gamma = bs_price_and_greeks(spot, strike, t_years, iv, self.r, opt_type)
        self.token_iv[token] = (ts, iv, gamma, delta, ltp, oi)

        now_dt = self.parse_ist_time(ts)
        if not self.is_time_gated(now_dt, self.start_time, self.end_time):
            return None

        # Check OTM Call vs OTM Put skew velocity
        # If this is an OTM Call (offset +1 or +2) or OTM Put (offset -1 or -2)
        if offset in (-2, -1, 1, 2):
            self._update_and_check_skew(inst, ts, spot)

        return None

    def _update_and_check_skew(self, inst: str, ts: float, spot: float):
        """Calculates 25-delta Risk Reversal / OTM Wing Skew and checks expansion velocity"""
        call_ivs = []
        put_ivs = []
        for t, val in self.token_iv.items():
            t_ts, t_iv, _, t_delta, _, _ = val
            if (ts - t_ts) < 90.0: # active within last 90 seconds
                if 0.15 <= t_delta <= 0.45: # OTM Call wing
                    call_ivs.append(t_iv)
                elif -0.45 <= t_delta <= -0.15: # OTM Put wing
                    put_ivs.append(t_iv)

        if not call_ivs or not put_ivs:
            return

        avg_call_iv = sum(call_ivs) / len(call_ivs) * 100.0
        avg_put_iv = sum(put_ivs) / len(put_ivs) * 100.0
        skew = avg_call_iv - avg_put_iv # Positive means Calls expensive relative to Puts

        hist = self.skew_history[inst]
        hist.append((ts, skew))
        while hist and (ts - hist[0][0]) > self.tau_sec:
            hist.popleft()

        if len(hist) < 5 or (ts - hist[0][0]) < 90.0:
            return

        baseline_skew = hist[0][1]
        delta_skew = skew - baseline_skew

        # Check for aggressive upside skew surge (Call bidding)
        if delta_skew >= self.skew_velocity_threshold:
            alert_key = f"IV_SKEW_CE_{inst}"
            self.emit_radar_alert(
                instrument=inst,
                direction="CE",
                alert_type="IV_SKEW_SURGE_CE",
                title=f"⚡ {inst} Institutional Call Wing IV Surge (+{delta_skew:.1f}%)",
                message=f"{inst} OTM Call IV surged from {baseline_skew:.1f}% to {skew:.1f}%. Smart money bidding up upside wing volatility 5-10m before spot breakout.",
                spot=spot,
                now_ts=ts,
                cooldown_key=alert_key
            )
        # Check for aggressive downside skew surge (Put bidding)
        elif delta_skew <= -self.skew_velocity_threshold:
            alert_key = f"IV_SKEW_PE_{inst}"
            self.emit_radar_alert(
                instrument=inst,
                direction="PE",
                alert_type="IV_SKEW_SURGE_PE",
                title=f"⚠️ {inst} Institutional Put Wing IV Surge ({delta_skew:.1f}%)",
                message=f"{inst} OTM Put IV expanded relative to Call IV (Skew: {skew:.1f}% vs baseline {baseline_skew:.1f}%). Heavy downside positioning detected.",
                spot=spot,
                now_ts=ts,
                cooldown_key=alert_key
            )
