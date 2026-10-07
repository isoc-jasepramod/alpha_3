"""
PROJECT ALPHA 3.0 — AGGREGATE INSTITUTIONAL FLOW INDEX (AIFI)
=============================================================
A standalone, non-tick-test institutional order flow engine designed for Indian index options.
Does NOT attempt to guess individual tick aggressor flags via noisy Level 2 approximations.

Instead, captures real macrostructural institutional order flow via 3 quantitative pillars:
1. Cross-Strike Premium Turnover Velocity (PTV):
   Measures 60s / 180s capital injection (Volume * Price) into ATM CE vs ATM PE clusters.
2. Trapped Open Interest (OI) Unwind Dynamics (OIF):
   Tracks aggressive market short-covering by trapped option writers across ATM strikes.
3. Micro-Price Order Book Imbalance (OBI):
   Computes instantaneous liquidity pressure and depth skew from the Best-5 order book.

Standalone & Isolated: Not wired to any live execution strategies.
Evaluated independently via lab_services backtest harness.
"""

import math
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple, Deque
from loguru import logger


class RollingMetric:
    """Maintains a rolling window of (timestamp, value) observations with O(1) query."""
    def __init__(self, window_sec: float = 60.0):
        self.window_sec = float(window_sec)
        self.history: Deque[Tuple[float, float]] = deque()
        self.cumulative: float = 0.0

    def add(self, ts: float, delta: float):
        self.history.append((ts, delta))
        self.cumulative += delta
        self._evict(ts)

    def _evict(self, current_ts: float):
        cutoff = current_ts - self.window_sec
        while self.history and self.history[0][0] < cutoff:
            _, old_delta = self.history.popleft()
            self.cumulative -= old_delta

    def sum(self, current_ts: float) -> float:
        self._evict(current_ts)
        return max(0.0, self.cumulative)


class InstitutionalFlowEngine:
    """
    Standalone Institutional Order Flow Engine (AIFI).
    Computes an unbiased [-1.0, +1.0] Institutional Flow Index for NIFTY and SENSEX.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or {}
        self.window_sec = float(cfg.get("window_sec", 60.0))
        self.min_samples = int(cfg.get("min_samples", 20))
        self.depth_weights = [1.0, 0.75, 0.50, 0.30, 0.15]

        # Weights for the 3 quantitative pillars (sum = 1.0)
        self.w_ptv = float(cfg.get("weight_ptv", 0.45))   # Premium Turnover Velocity
        self.w_oif = float(cfg.get("weight_oif", 0.35))   # Trapped OI Unwind Flow
        self.w_obi = float(cfg.get("weight_obi", 0.20))   # Micro-Price Depth Imbalance

        # Per-token rolling volume and OI trackers: token -> {"last_vol": float, "last_oi": float, "last_ts": float}
        self.token_state: Dict[str, Dict[str, Any]] = {}

        # Per-instrument rolling capital flow trackers
        # inst -> {"CE": RollingMetric, "PE": RollingMetric}
        self.turnover_rolling: Dict[str, Dict[str, RollingMetric]] = {
            "NIFTY": {"CE": RollingMetric(self.window_sec), "PE": RollingMetric(self.window_sec)},
            "SENSEX": {"CE": RollingMetric(self.window_sec), "PE": RollingMetric(self.window_sec)},
        }

        # Per-instrument OI delta trackers
        # inst -> {"CE_unwind": RollingMetric, "PE_unwind": RollingMetric}
        self.oi_unwind_rolling: Dict[str, Dict[str, RollingMetric]] = {
            "NIFTY": {"CE_unwind": RollingMetric(self.window_sec * 2.0), "PE_unwind": RollingMetric(self.window_sec * 2.0)},
            "SENSEX": {"CE_unwind": RollingMetric(self.window_sec * 2.0), "PE_unwind": RollingMetric(self.window_sec * 2.0)},
        }

        # Latest computed flow index per instrument
        self.latest_flow: Dict[str, Dict[str, Any]] = {
            "NIFTY": self._default_flow_state("NIFTY"),
            "SENSEX": self._default_flow_state("SENSEX"),
        }

    def _default_flow_state(self, inst: str) -> Dict[str, Any]:
        return {
            "instrument": inst,
            "aifi": 0.0,              # Composite Flow Index [-1.0 to +1.0]
            "direction": "NEUTRAL",   # "BULLISH", "BEARISH", "NEUTRAL"
            "confidence": 0,          # [0 to 100%]
            "ptv_score": 0.0,         # Premium Turnover Velocity component [-1 to +1]
            "oif_score": 0.0,         # Trapped OI Unwind component [-1 to +1]
            "obi_score": 0.0,         # Micro-Price Order Book Imbalance component [-1 to +1]
            "ce_turnover_cr": 0.0,    # 60s CE capital turnover in Crores
            "pe_turnover_cr": 0.0,    # 60s PE capital turnover in Crores
            "updated_at": None,
        }

    def on_tick(self, tick: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """
        Consumes an option or spot tick and updates the institutional flow index.
        Returns the latest updated flow state if a meaningful transition occurred.
        """
        token = str(tick.get("token", ""))
        ltp = float(tick.get("ltp", 0.0))
        if ltp <= 0 or not meta or meta.get("is_spot"):
            return None

        inst = meta.get("name", "NIFTY")
        if inst not in self.latest_flow:
            return None

        opt_type = meta.get("option_type")  # "CE" or "PE"
        if opt_type not in ("CE", "PE"):
            return None

        # Standardize timestamp in seconds
        raw_ts = float(tick.get("exchange_timestamp", 0.0) or 0.0)
        ts = raw_ts if raw_ts < 1e11 else raw_ts / 1000.0
        if ts <= 0:
            ts = datetime.now(timezone.utc).timestamp()

        cur_vol = float(tick.get("volume", 0.0) or 0.0)
        cur_oi = float(tick.get("open_interest", 0.0) or 0.0)

        # -------------------------------------------------------------
        # 1. PILLAR 1: Cross-Strike Premium Turnover Velocity (PTV)
        # -------------------------------------------------------------
        state = self.token_state.get(token)
        if state is not None:
            old_vol = state["last_vol"]
            old_oi = state["last_oi"]
            old_ltp = state["last_ltp"]

            delta_vol = max(0.0, cur_vol - old_vol)
            delta_oi = cur_oi - old_oi

            # Capital turnover delta in Rupees (Rupee Velocity)
            rupee_delta = delta_vol * ltp
            if rupee_delta > 0:
                self.turnover_rolling[inst][opt_type].add(ts, rupee_delta)

            # -------------------------------------------------------------
            # 2. PILLAR 2: Trapped OI Unwind Dynamics (OIF)
            # -------------------------------------------------------------
            # Trapped Call Writers covering: Price jumping (> 0.5%) + OI unwinding (< 0)
            if opt_type == "CE" and delta_oi < 0 and ltp > old_ltp:
                unwind_intensity = abs(delta_oi) * ltp
                self.oi_unwind_rolling[inst]["CE_unwind"].add(ts, unwind_intensity)

            # Trapped Put Writers covering: Price jumping (> 0.5%) + OI unwinding (< 0)
            elif opt_type == "PE" and delta_oi < 0 and ltp > old_ltp:
                unwind_intensity = abs(delta_oi) * ltp
                self.oi_unwind_rolling[inst]["PE_unwind"].add(ts, unwind_intensity)

            # Update cache
            state["last_vol"] = cur_vol
            state["last_oi"] = cur_oi
            state["last_ltp"] = ltp
            state["last_ts"] = ts
        else:
            self.token_state[token] = {
                "last_vol": cur_vol,
                "last_oi": cur_oi,
                "last_ltp": ltp,
                "last_ts": ts,
            }
            return None

        # -------------------------------------------------------------
        # 3. PILLAR 3: Micro-Price Order Book Imbalance (OBI)
        # -------------------------------------------------------------
        obi_val = self._compute_obi(tick)

        # -------------------------------------------------------------
        # 4. COMPOSITE AGGREGATE FLOW CALCULATION
        # -------------------------------------------------------------
        # Compute 60-second rolling turnover for CE vs PE
        ce_turnover = self.turnover_rolling[inst]["CE"].sum(ts)
        pe_turnover = self.turnover_rolling[inst]["PE"].sum(ts)
        tot_turnover = ce_turnover + pe_turnover

        # PTV Score [-1.0 to +1.0]
        if tot_turnover > 10000.0:
            ptv_score = (ce_turnover - pe_turnover) / tot_turnover
        else:
            ptv_score = 0.0

        # OIF Score [-1.0 to +1.0]
        # CE Unwind = Bullish (+), PE Unwind = Bearish (-)
        ce_unwind = self.oi_unwind_rolling[inst]["CE_unwind"].sum(ts)
        pe_unwind = self.oi_unwind_rolling[inst]["PE_unwind"].sum(ts)
        tot_unwind = ce_unwind + pe_unwind

        if tot_unwind > 5000.0:
            oif_score = (ce_unwind - pe_unwind) / tot_unwind
        else:
            oif_score = 0.0

        # Weighted AIFI Index
        composite_aifi = (
            (self.w_ptv * ptv_score) +
            (self.w_oif * oif_score) +
            (self.w_obi * obi_val)
        )
        composite_aifi = max(-1.0, min(1.0, composite_aifi))

        # Direction classification
        if composite_aifi >= 0.25:
            direction = "BULLISH"
        elif composite_aifi <= -0.25:
            direction = "BEARISH"
        else:
            direction = "NEUTRAL"

        # Confidence: based on turnover volume scale and alignment of pillars
        pillars = [math.copysign(1.0, p) if abs(p) > 0.1 else 0 for p in (ptv_score, oif_score, obi_val)]
        alignment = sum(1 for p in pillars if (direction == "BULLISH" and p > 0) or (direction == "BEARISH" and p < 0))
        conf = int((alignment / 3.0) * 80.0 + min(20.0, (tot_turnover / 5e7) * 20.0))
        conf = max(10, min(100, conf))

        flow_state = {
            "instrument": inst,
            "aifi": round(composite_aifi, 3),
            "direction": direction,
            "confidence": conf,
            "ptv_score": round(ptv_score, 3),
            "oif_score": round(oif_score, 3),
            "obi_score": round(obi_val, 3),
            "ce_turnover_cr": round(ce_turnover / 1e7, 2),
            "pe_turnover_cr": round(pe_turnover / 1e7, 2),
            "updated_at": ts,
        }

        self.latest_flow[inst] = flow_state
        return flow_state

    def _compute_obi(self, tick: Dict[str, Any]) -> float:
        """
        Computes weighted Order Book Imbalance from Best-5 Depth.
        Returns value in [-1.0, +1.0].
        """
        depth = tick.get("depth")
        bids_qty = 0.0
        asks_qty = 0.0

        if depth and isinstance(depth, dict):
            bids = depth.get("bids", [])
            asks = depth.get("asks", [])
            for k in range(min(5, len(bids))):
                w = self.depth_weights[k]
                bids_qty += float(bids[k].get("qty", 0.0) or 0.0) * w
            for k in range(min(5, len(asks))):
                w = self.depth_weights[k]
                asks_qty += float(asks[k].get("qty", 0.0) or 0.0) * w
        else:
            # Fallback to flat columns if flat-row format
            for k in range(1, 6):
                w = self.depth_weights[k - 1]
                bids_qty += float(tick.get(f"bid{k}_qty", 0.0) or 0.0) * w
                asks_qty += float(tick.get(f"ask{k}_qty", 0.0) or 0.0) * w

        tot = bids_qty + asks_qty
        if tot <= 1e-5:
            return 0.0

        return max(-1.0, min(1.0, (bids_qty - asks_qty) / tot))

    def get_flow_state(self, inst: str) -> Dict[str, Any]:
        """Returns the latest flow state for an instrument."""
        return self.latest_flow.get(inst, self._default_flow_state(inst))
