"""
PROJECT ALPHA 3.0 — ALERT CATEGORY TESTS

Verifies every radar alert carries a CONTEXT / WATCH / ACTIONABLE category so the UI can
tier them and cut alert fatigue. Background positioning signals (PCR, IV skew) must be
CONTEXT; near-trigger cues (pullback ready, ignition) must be ACTIONABLE.
"""

import pytest
from backend.strategies.base_strategy import (
    BaseStrategy,
    categorize_alert,
    ALERT_CATEGORY_CONTEXT,
    ALERT_CATEGORY_WATCH,
    ALERT_CATEGORY_ACTIONABLE,
)


class _Dummy(BaseStrategy):
    async def on_tick(self, tick, meta=None):
        return None


def test_context_alerts_are_context():
    for at in ("CHAIN_PCR_VELOCITY_PE", "CHAIN_PCR_VELOCITY_CE",
               "IV_SKEW_SURGE_CE", "IV_SKEW_SURGE_PE",
               "ZERO_GAMMA_FLIP", "VOLATILITY_COIL_ACTIVE"):
        assert categorize_alert(at) == ALERT_CATEGORY_CONTEXT, at


def test_actionable_alerts_are_actionable():
    for at in ("MOMENTUM_PULLBACK_READY", "SQUEEZE_BREAKOUT_FIRING",
               "DRYUP_IGNITION", "ORB_DRYUP_IGNITION", "VOLUME_DRYUP_SQUEEZE"):
        assert categorize_alert(at) == ALERT_CATEGORY_ACTIONABLE, at


def test_watch_is_default_for_unknown():
    assert categorize_alert("SOME_FUTURE_ALERT") == ALERT_CATEGORY_WATCH
    assert categorize_alert("CONFLUENCE_PRE_ENTRY") == ALERT_CATEGORY_WATCH


def test_emit_radar_alert_injects_category():
    s = _Dummy(name="TEST")
    alert = s.emit_radar_alert(
        alert_type="CHAIN_PCR_VELOCITY_PE",
        instrument="NIFTY",
        direction="PE",
        title="t",
        message="m",
        now_ts=1000.0
    )
    assert alert is not None
    assert alert["category"] == ALERT_CATEGORY_CONTEXT

    alert2 = s.emit_radar_alert(
        alert_type="MOMENTUM_PULLBACK_READY",
        instrument="SENSEX",
        direction="CE",
        title="t",
        message="m",
        now_ts=2000.0
    )
    assert alert2["category"] == ALERT_CATEGORY_ACTIONABLE
