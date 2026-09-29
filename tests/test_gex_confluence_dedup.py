"""
PROJECT ALPHA 3.0 — GEX CONFLUENCE ALERT DEDUP TESTS

Regression coverage for the Sep 29 2026 CONFLUENCE_PRE_ENTRY spam: the alert is a
persisting STATE (spot near a gamma wall + IV skew surge), so with only the 75s
emit_radar_alert cooldown it re-fired repeatedly while spot loitered near the wall.

The fix latches the active confluence setup by identity (direction + wall strike) and
only re-alerts when a materially different setup forms after the prior one clears.
"""

import pytest
from backend.strategies.gex_engine import GEXEngine


class StubIV:
    """Minimal IV engine stub returning a fixed bullish skew and no per-token greeks."""
    def __init__(self, skew_delta=2.0, skew=2.5):
        self._skew_delta = skew_delta
        self._skew = skew

    def get_token_greeks(self, token):
        return None  # force GEXEngine to compute gamma locally

    def get_latest_skew_delta(self, inst):
        return self._skew_delta

    def get_latest_skew(self, inst):
        return self._skew


def _seed_call_wall_chain(engine, inst, spot, wall_strike):
    """
    Build a chain where wall_strike carries dominant CALL gamma exposure so the engine
    picks it as the call wall. Uses several strikes so len(strikes) >= 3.
    """
    engine.spot_prices[inst] = spot
    step = 50.0 if inst == "NIFTY" else 100.0
    lot = 50 if inst == "NIFTY" else 10
    engine.chain_data[inst] = {}
    for k in [wall_strike - step, wall_strike, wall_strike + step]:
        # Big CALL OI/gamma at the wall strike, small elsewhere.
        ce_oi = 500000.0 if k == wall_strike else 50000.0
        engine.chain_data[inst][k] = {
            "CE": {"token": f"ce{int(k)}", "oi": ce_oi, "gamma": 0.02, "ltp": 100.0, "lot_size": lot, "ts": 0.0},
            "PE": {"token": f"pe{int(k)}", "oi": 40000.0, "gamma": 0.01, "ltp": 100.0, "lot_size": lot, "ts": 0.0},
        }


def test_confluence_alerts_once_while_setup_persists():
    """Repeated evaluations of the SAME confluence setup must emit exactly one alert."""
    engine = GEXEngine(iv_engine=StubIV())
    inst = "NIFTY"
    # Spot 20 pts below a call wall at 22750 => within 35pt proximity, bullish skew.
    _seed_call_wall_chain(engine, inst, spot=22730.0, wall_strike=22750.0)

    ts = 1000.0
    for i in range(10):
        engine._evaluate_gamma_regime(inst, ts + i * 5.0, engine.spot_prices[inst])

    confluence_alerts = [a for a in engine.pending_alerts if a["alert_type"] == "CONFLUENCE_PRE_ENTRY"]
    assert len(confluence_alerts) == 1, (
        f"Persisting setup should alert once, got {len(confluence_alerts)}"
    )
    # The latch should reflect the active setup.
    assert engine._confluence_active[inst] == {"direction": "CE", "wall": 22750.0}


def test_confluence_realerts_after_setup_clears_and_reforms():
    """Setup forms -> clears (spot moves away) -> re-forms should produce a SECOND alert."""
    engine = GEXEngine(iv_engine=StubIV())
    inst = "NIFTY"
    _seed_call_wall_chain(engine, inst, spot=22730.0, wall_strike=22750.0)

    # 1. Form + alert.
    engine._evaluate_gamma_regime(inst, 1000.0, 22730.0)
    # 2. Spot moves far from the wall -> confluence no longer holds -> latch clears.
    engine._evaluate_gamma_regime(inst, 1005.0, 22600.0)
    assert engine._confluence_active[inst] is None
    # 3. Spot returns to the wall past the 75s base emit cooldown -> fresh setup -> new alert.
    #    (The base emit_radar_alert cooldown is a secondary time backstop; a re-form within
    #    75s would still be suppressed by it, which is acceptable — that's noise anyway.)
    engine._evaluate_gamma_regime(inst, 1090.0, 22730.0)

    confluence_alerts = [a for a in engine.pending_alerts if a["alert_type"] == "CONFLUENCE_PRE_ENTRY"]
    assert len(confluence_alerts) == 2, (
        f"Cleared-then-reformed setup should alert twice, got {len(confluence_alerts)}"
    )


def test_is_new_confluence_identity_logic():
    """Direct unit check of the dedup identity rule."""
    engine = GEXEngine(iv_engine=StubIV())
    inst = "NIFTY"

    # Nothing latched: any setup is new.
    assert engine._is_new_confluence(inst, "CE", 22750.0) is True

    engine._confluence_active[inst] = {"direction": "CE", "wall": 22750.0}
    # Same direction + same wall (within tolerance 50): NOT new.
    assert engine._is_new_confluence(inst, "CE", 22750.0) is False
    assert engine._is_new_confluence(inst, "CE", 22770.0) is False  # 20pt drift within tolerance
    # Wall shifted beyond tolerance: new.
    assert engine._is_new_confluence(inst, "CE", 22850.0) is True
    # Opposite direction: new.
    assert engine._is_new_confluence(inst, "PE", 22750.0) is True
