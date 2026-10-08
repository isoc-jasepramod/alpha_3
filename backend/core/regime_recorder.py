"""
PROJECT ALPHA 3.0 — REGIME COMPARISON RECORDER
===============================================
Writes one JSONL row per spot-candle-close capturing BOTH regime filters (v1 live gating filter
and v2 A/B candidate) plus the breadth snapshot used by v2. This is what makes next-day A/B
comparison possible — without a recorded trace, v2 is invisible.

Output: data/regime_compare/date=YYYY-MM-DD/<INST>.jsonl   (IST calendar date)

Each row is self-describing (schema versioned) so the report script can evolve independently.
Writing is best-effort and never raises into the hot path.
"""
import os
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional
from loguru import logger

IST = timezone(timedelta(hours=5, minutes=30))
SCHEMA_VERSION = 1


class RegimeComparisonRecorder:
    def __init__(self, base_dir: Optional[str] = None):
        if base_dir is None:
            base_dir = os.path.join(
                os.path.dirname(__file__), "..", "..", "data", "regime_compare"
            )
        self.base_dir = os.path.abspath(base_dir)
        self._warned = False

    def _path_for(self, inst: str, now_ist: datetime) -> str:
        day = now_ist.strftime("%Y-%m-%d")
        d = os.path.join(self.base_dir, f"date={day}")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{inst}.jsonl")

    def record(
        self,
        inst: str,
        candle: Dict[str, Any],
        v1_state: Dict[str, Any],
        v2_state: Dict[str, Any],
        breadth: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            now_ist = datetime.now(IST)
            row = {
                "schema": SCHEMA_VERSION,
                "ts_ist": now_ist.isoformat(),
                "inst": inst,
                "candle": {
                    "open": candle.get("open"),
                    "high": candle.get("high"),
                    "low": candle.get("low"),
                    "close": candle.get("close"),
                    "ts": candle.get("timestamp") or candle.get("ts"),
                },
                "v1": {
                    "regime": v1_state.get("regime"),
                    "direction": v1_state.get("direction"),
                    "score": v1_state.get("score"),
                    "adx": v1_state.get("adx_value") or v1_state.get("adx"),
                    "er": v1_state.get("er_value") or v1_state.get("er"),
                },
                "v2": {
                    "regime": v2_state.get("regime"),
                    "direction": v2_state.get("direction"),
                    "health": v2_state.get("health"),
                    "score": v2_state.get("score"),
                    "trend_quality": v2_state.get("trend_quality"),
                    "participation_adj": v2_state.get("participation_adj"),
                    "adx": v2_state.get("adx_value"),
                    "plus_di": v2_state.get("plus_di"),
                    "minus_di": v2_state.get("minus_di"),
                    "er": v2_state.get("er_value"),
                    "slope_norm": v2_state.get("vwap_slope_norm"),
                    "persistence": v2_state.get("persistence_score"),
                },
                "breadth": None if not breadth else {
                    "valid": breadth.get("valid"),
                    "breadth_pct": breadth.get("breadth_pct"),
                    "weighted_pressure_pct": breadth.get("weighted_pressure_pct"),
                    "advances": breadth.get("advances"),
                    "declines": breadth.get("declines"),
                    "spoof": breadth.get("spoof_detected"),
                },
            }
            path = self._path_for(inst, now_ist)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except Exception as e:
            if not self._warned:
                logger.warning(f"[REGIME-REC] record failed (further warnings suppressed): {e}")
                self._warned = True
