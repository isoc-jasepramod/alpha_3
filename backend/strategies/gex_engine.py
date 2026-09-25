import math
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple, List
from collections import deque
from loguru import logger
from backend.strategies.base_strategy import BaseStrategy
from backend.strategies.iv_engine import IVEngine, bs_price_and_greeks, solve_iv

class GEXEngine(BaseStrategy):
    """
    Dealer Gamma Exposure (GEX) Engine.
    Tracks strike-level Net Gamma, Dealer Zero-Gamma Flip Level, and Gamma Squeeze Walls.
    Flags explosive mechanical dealer hedging zones BEFORE runaway impulse spikes occur.
    """

    def __init__(self, iv_engine: Optional[IVEngine] = None, config: Optional[Dict[str, Any]] = None):
        super().__init__(name="GEX_ENGINE")
        self.iv_engine = iv_engine
        cfg = config or {}
        self.start_time = cfg.get("start_time", "09:20:00")
        self.end_time = cfg.get("end_time", "15:25:00")
        self.r = cfg.get("risk_free_rate", 0.07)
        self.spot_prices: Dict[str, float] = {"NIFTY": 0.0, "SENSEX": 0.0}

        # Strike data per instrument: inst -> strike -> {"CE": (token, oi, gamma, ltp), "PE": (token, oi, gamma, ltp)}
        self.chain_data: Dict[str, Dict[float, Dict[str, Any]]] = {
            "NIFTY": {},
            "SENSEX": {}
        }

        # Rolling GEX regime tracking
        self.prev_regimes: Dict[str, str] = {"NIFTY": "NEUTRAL", "SENSEX": "NEUTRAL"}
        self.last_eval_ts: Dict[str, float] = {"NIFTY": 0.0, "SENSEX": 0.0}

    def _get_time_to_expiry_years(self, ts: float, inst: str = "NIFTY") -> float:
        dt = self.parse_ist_time(ts)
        exp_weekday = 1 if inst == "NIFTY" else 3  # Tuesday=1 for NIFTY, Thursday=3 for SENSEX
        days_ahead = (exp_weekday - dt.weekday()) % 7
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

    async def on_tick(self, tick: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        token = str(tick.get("token", ""))
        ltp = float(tick.get("ltp", 0.0))
        oi = float(tick.get("open_interest", 0.0))
        raw_ts = tick.get("exchange_timestamp") or datetime.now(timezone.utc).timestamp()
        ts = float(raw_ts) if raw_ts > 1e11 else float(raw_ts)
        if ts > 1e11:
            ts /= 1000.0

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
        lot_size = int(meta.get("lot_size", 50 if inst == "NIFTY" else 10))

        # Retrieve gamma from iv_engine or compute locally
        gamma = 0.0
        if self.iv_engine:
            greeks = self.iv_engine.get_token_greeks(token)
            if greeks:
                gamma = greeks[1]

        if gamma <= 0:
            t_years = self._get_time_to_expiry_years(ts)
            iv = solve_iv(ltp, spot, strike, t_years, self.r, opt_type) or 0.18
            _, _, gamma = bs_price_and_greeks(spot, strike, t_years, iv, self.r, opt_type)

        # Store in chain_data
        if inst not in self.chain_data:
            self.chain_data[inst] = {}
        if strike not in self.chain_data[inst]:
            self.chain_data[inst][strike] = {}

        self.chain_data[inst][strike][opt_type] = {
            "token": token,
            "oi": oi,
            "gamma": gamma,
            "ltp": ltp,
            "lot_size": lot_size,
            "ts": ts
        }

        # Evaluate GEX regime every 5 seconds
        if (ts - self.last_eval_ts.get(inst, 0.0)) >= 5.0:
            self.last_eval_ts[inst] = ts
            self._evaluate_gamma_regime(inst, ts, spot)

        return None

    def _evaluate_gamma_regime(self, inst: str, ts: float, spot: float):
        strikes = self.chain_data.get(inst, {})
        if len(strikes) < 3:
            return

        total_call_gex = 0.0
        total_put_gex = 0.0
        call_wall_strike = 0.0
        put_wall_strike = 0.0
        max_call_gex = -1.0
        max_put_gex = -1.0

        for strike, data in strikes.items():
            ce = data.get("CE")
            pe = data.get("PE")

            # Call GEX: Dealer is short call when retail is long -> Hedging creates upward force
            if ce and ce["oi"] > 0 and ce["gamma"] > 0:
                # GEX in Crores INR = OI * Gamma * Spot^2 * LotSize / 1e7
                gex_ce = (ce["oi"] * ce["gamma"] * (spot ** 2) * ce["lot_size"]) / 1e7
                total_call_gex += gex_ce
                if gex_ce > max_call_gex:
                    max_call_gex = gex_ce
                    call_wall_strike = strike

            # Put GEX: Dealer is short put -> Hedging creates downward acceleration
            if pe and pe["oi"] > 0 and pe["gamma"] > 0:
                gex_pe = (pe["oi"] * pe["gamma"] * (spot ** 2) * pe["lot_size"]) / 1e7
                total_put_gex += gex_pe
                if gex_pe > max_put_gex:
                    max_put_gex = gex_pe
                    put_wall_strike = strike

        # Net GEX = Call GEX - Put GEX
        net_gex = total_call_gex - total_put_gex
        current_regime = "SHORT_GAMMA_AMPLIFIER" if net_gex < -5.0 else ("LONG_GAMMA_DAMPENER" if net_gex > 5.0 else "NEUTRAL")

        prev = self.prev_regimes.get(inst, "NEUTRAL")
        self.prev_regimes[inst] = current_regime

        # Alert 1: Zero-Gamma Flip (Market Maker regime switches to amplifier)
        if prev == "LONG_GAMMA_DAMPENER" and current_regime == "SHORT_GAMMA_AMPLIFIER":
            self.emit_radar_alert(
                alert_type="ZERO_GAMMA_FLIP",
                instrument=inst,
                direction="PE" if spot < put_wall_strike else "CE",
                title=f"⚡ {inst} Zero-Gamma Flip: Market Entering High-Volatility Amplifier Zone",
                message=f"{inst} Net GEX flipped to {net_gex:.1f} Cr. Market maker delta hedging will now amplify moves instead of dampening them. Prepare for violent spikes.",
                meta_details={"spot": spot, "net_gex": round(net_gex, 2), "call_wall": call_wall_strike, "put_wall": put_wall_strike},
                now_ts=ts
            )

        # Check for GEX + IV Skew Confluence (Elevated Radar Alert)
        delta_skew = self.iv_engine.get_latest_skew_delta(inst) if self.iv_engine else None
        current_skew = self.iv_engine.get_latest_skew(inst) if self.iv_engine else None
        proximity_pts = 35.0 if inst == "NIFTY" else 100.0
        sl_pts = 25.0 if inst == "NIFTY" else 75.0
        target_1_pts = 35.0 if inst == "NIFTY" else 100.0
        target_2_pts = 75.0 if inst == "NIFTY" else 220.0

        is_call_confluence = (
            call_wall_strike > 0 and 
            0 < (call_wall_strike - spot) <= proximity_pts and
            ((delta_skew is not None and delta_skew >= 1.5) or (current_skew is not None and current_skew >= 2.0))
        )

        is_put_confluence = (
            put_wall_strike > 0 and 
            0 < (spot - put_wall_strike) <= proximity_pts and
            ((delta_skew is not None and delta_skew <= -1.5) or (current_skew is not None and current_skew <= -2.0))
        )

        # Elevated Confluence Alert (CE): Call Gamma Wall + Institutional Call IV Skew Surge
        if is_call_confluence:
            spot_sl = spot - sl_pts
            t1 = call_wall_strike + target_1_pts
            t2 = call_wall_strike + target_2_pts
            skew_str = f"+{delta_skew:.1f}%" if delta_skew is not None else "Elevated"
            self.emit_radar_alert(
                alert_type="CONFLUENCE_PRE_ENTRY",
                instrument=inst,
                direction="CE",
                title=f"🎯 ELEVATED RADAR: {inst} Pre-Breakout Confluence @ {call_wall_strike:.0f} CE",
                message=f"High-conviction confluence: {inst} approaching Call Wall ({call_wall_strike:.0f}) with Call IV Skew expansion ({skew_str}). Plan: Strike {call_wall_strike:.0f} CE | Spot SL: {spot_sl:.1f} | T1: {t1:.0f} | T2: {t2:.0f}",
                meta_details={
                    "spot": spot,
                    "entry_strike": call_wall_strike,
                    "recommended_sl": round(spot_sl, 2),
                    "target_1": round(t1, 2),
                    "target_2": round(t2, 2),
                    "call_wall": call_wall_strike,
                    "delta_skew": round(delta_skew, 2) if delta_skew is not None else 0.0,
                    "net_gex": round(net_gex, 2),
                    "confluence_type": "GEX_CALL_WALL + CALL_IV_SKEW_SURGE"
                },
                now_ts=ts
            )
        # Elevated Confluence Alert (PE): Put Gamma Wall + Institutional Put IV Skew Surge
        elif is_put_confluence:
            spot_sl = spot + sl_pts
            t1 = put_wall_strike - target_1_pts
            t2 = put_wall_strike - target_2_pts
            skew_str = f"{delta_skew:.1f}%" if delta_skew is not None else "Elevated"
            self.emit_radar_alert(
                alert_type="CONFLUENCE_PRE_ENTRY",
                instrument=inst,
                direction="PE",
                title=f"🎯 ELEVATED RADAR: {inst} Pre-Breakdown Confluence @ {put_wall_strike:.0f} PE",
                message=f"High-conviction confluence: {inst} approaching Put Wall ({put_wall_strike:.0f}) with Put IV Skew expansion ({skew_str}). Plan: Strike {put_wall_strike:.0f} PE | Spot SL: {spot_sl:.1f} | T1: {t1:.0f} | T2: {t2:.0f}",
                meta_details={
                    "spot": spot,
                    "entry_strike": put_wall_strike,
                    "recommended_sl": round(spot_sl, 2),
                    "target_1": round(t1, 2),
                    "target_2": round(t2, 2),
                    "put_wall": put_wall_strike,
                    "delta_skew": round(delta_skew, 2) if delta_skew is not None else 0.0,
                    "net_gex": round(net_gex, 2),
                    "confluence_type": "GEX_PUT_WALL + PUT_IV_SKEW_SURGE"
                },
                now_ts=ts
            )
        # Alert 2: Proximity to Call Wall under Short Gamma (Classic Gamma Squeeze Setup)
        elif call_wall_strike > 0 and 0 < (call_wall_strike - spot) <= (30.0 if inst == "NIFTY" else 90.0):
            # Spot is within 30 points of Call Wall
            self.emit_radar_alert(
                alert_type="GAMMA_SQUEEZE_PRE_ALERT",
                instrument=inst,
                direction="CE",
                title=f"🚀 {inst} Gamma Squeeze Wall Alert @ {call_wall_strike:.0f}",
                message=f"{inst} (Spot: {spot:.1f}) is within striking distance of Major Call Gamma Wall ({call_wall_strike:.0f}). Piercing this strike forces dealer short covering cascade.",
                meta_details={"spot": spot, "call_wall": call_wall_strike, "net_gex": round(net_gex, 2)},
                now_ts=ts
            )
        # Alert 3: Proximity to Put Wall (Downside Cascade)
        elif put_wall_strike > 0 and 0 < (spot - put_wall_strike) <= (30.0 if inst == "NIFTY" else 90.0):
            self.emit_radar_alert(
                alert_type="GAMMA_CASCADE_PE_ALERT",
                instrument=inst,
                direction="PE",
                title=f"🔻 {inst} Downside Gamma Cascade Alert @ {put_wall_strike:.0f}",
                message=f"{inst} (Spot: {spot:.1f}) approaching Put Gamma Wall ({put_wall_strike:.0f}). Breaking below triggers forced dealer short futures selling.",
                meta_details={"spot": spot, "put_wall": put_wall_strike, "net_gex": round(net_gex, 2)},
                now_ts=ts
            )
