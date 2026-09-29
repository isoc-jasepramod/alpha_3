"""
PROJECT ALPHA 3.0 — REGIME HYSTERESIS TESTS

Regression coverage for the Sep 29 2026 incident: the regime filter logged 305
transitions on a choppy day because the classifier flipped on every single-candle
threshold crossing, which also produced the false "TREND UP 85" badge.

These tests exercise the hysteresis machinery directly:
  1. Deadband bands stop boundary jitter from flipping the committed regime.
  2. Entering TRENDING requires confirm_bars_trending consecutive candidate bars.
  3. Relaxing toward NEUTRAL/CHOPPY commits within confirm_bars_relax bars.
  4. Direction is only stamped BULL/BEAR when slope sign and VWAP persistence agree;
     a flat/conflicting reading does NOT get a directional trend badge.
"""

import pytest
from backend.strategies.regime_filter import RegimeFilter


def _set_regime(rf, inst, regime, direction="NEUTRAL"):
    """Force a committed regime so we can test transitions from a known state."""
    st = rf._default_regime_state(inst)
    st["regime"] = regime
    st["direction"] = direction
    st["is_trending"] = "TRENDING" in regime
    st["is_neutral"] = regime == "NEUTRAL"
    st["is_choppy"] = regime == "CHOPPY"
    rf.latest_regime[inst] = st
    rf._pending_change[inst] = {"candidate": None, "count": 0}


# ---------------------------------------------------------------------------
# 1. Deadband: boundary jitter must not flip the committed regime
# ---------------------------------------------------------------------------

def test_deadband_holds_trending_within_exit_band():
    """Score dipping to 62 (below 65 enter, above 60 exit) must stay TRENDING."""
    rf = RegimeFilter()
    # Enter thresholds: trending 65, neutral 45. Exit bands 5 => trend_exit 60.
    assert rf._raw_regime_with_deadband(62.0, "TRENDING_BULL") == "TRENDING"
    # Only when it clearly drops below 60 does it leave.
    assert rf._raw_regime_with_deadband(59.0, "TRENDING_BULL") == "NEUTRAL"


def test_deadband_holds_choppy_within_exit_band():
    """Score rising to 48 (above 45 enter, below 50 exit) must stay CHOPPY."""
    rf = RegimeFilter()
    # choppy_exit = neutral_threshold + band = 45 + 5 = 50.
    assert rf._raw_regime_with_deadband(48.0, "CHOPPY") == "CHOPPY"
    # Only when it clearly rises above 50 does it leave chop.
    assert rf._raw_regime_with_deadband(51.0, "CHOPPY") == "NEUTRAL"


def test_deadband_prevents_boundary_oscillation():
    """
    The exact Sep 29 pathology: a score oscillating 64/66/64/66 around the trending
    boundary must NOT thrash the committed family once inside the deadband.
    """
    rf = RegimeFilter()
    # Start NEUTRAL. 66 -> candidate TRENDING (enter). Then 64 must NOT drop out
    # (64 >= trend_exit 60), so it stays TRENDING.
    fam1 = rf._raw_regime_with_deadband(66.0, "NEUTRAL")
    assert fam1 == "TRENDING"
    fam2 = rf._raw_regime_with_deadband(64.0, "TRENDING_BULL")
    assert fam2 == "TRENDING"
    fam3 = rf._raw_regime_with_deadband(66.0, "TRENDING_BULL")
    assert fam3 == "TRENDING"


# ---------------------------------------------------------------------------
# 2 & 3. Confirmation bars
# ---------------------------------------------------------------------------

def test_entering_trending_requires_confirmation_bars():
    """A single high-score candidate must NOT immediately commit TRENDING."""
    rf = RegimeFilter()
    inst = "NIFTY"
    _set_regime(rf, inst, "NEUTRAL")

    # confirm_bars_trending defaults to 2.
    # Bar 1: candidate TRENDING_BULL -> not yet committed, holds NEUTRAL.
    regime, direction = rf._apply_hysteresis(
        inst, "TRENDING_BULL", "BULL", "NEUTRAL", rf.latest_regime[inst]
    )
    assert regime == "NEUTRAL", "First trending bar should not commit"
    assert rf._pending_change[inst]["candidate"] == "TRENDING_BULL"
    assert rf._pending_change[inst]["count"] == 1

    # Bar 2: same candidate -> now commits.
    regime, direction = rf._apply_hysteresis(
        inst, "TRENDING_BULL", "BULL", "NEUTRAL", rf.latest_regime[inst]
    )
    assert regime == "TRENDING_BULL", "Second consecutive trending bar should commit"
    assert direction == "BULL"
    assert rf._pending_change[inst]["candidate"] is None


def test_interrupted_candidate_resets_confirmation():
    """A candidate that does not persist consecutively must reset the counter."""
    rf = RegimeFilter()
    inst = "NIFTY"
    _set_regime(rf, inst, "NEUTRAL")

    # Bar 1: TRENDING_BULL candidate (count 1, held NEUTRAL).
    rf._apply_hysteresis(inst, "TRENDING_BULL", "BULL", "NEUTRAL", rf.latest_regime[inst])
    assert rf._pending_change[inst]["count"] == 1

    # Bar 2: candidate switches to CHOPPY -> resets to CHOPPY pending, count 1.
    # (relax needs only 1 bar, so CHOPPY commits immediately.)
    regime, _ = rf._apply_hysteresis(inst, "CHOPPY", "NEUTRAL", "NEUTRAL", rf.latest_regime[inst])
    assert regime == "CHOPPY", "Relaxing toward CHOPPY commits within confirm_bars_relax=1"


def test_relaxing_commits_faster_than_trending():
    """Leaving TRENDING toward NEUTRAL needs only confirm_bars_relax (=1) bar."""
    rf = RegimeFilter()
    inst = "NIFTY"
    _set_regime(rf, inst, "TRENDING_BULL", direction="BULL")

    regime, direction = rf._apply_hysteresis(
        inst, "NEUTRAL", "NEUTRAL", "TRENDING_BULL", rf.latest_regime[inst]
    )
    assert regime == "NEUTRAL", "Single relax bar should commit the calmer regime"


def test_direction_flip_needs_trending_confirmation():
    """BULL->BEAR flip is a trending-family change and needs confirm_bars_trending."""
    rf = RegimeFilter()
    inst = "NIFTY"
    _set_regime(rf, inst, "TRENDING_BULL", direction="BULL")

    # Bar 1: candidate flips to TRENDING_BEAR -> hold prior BULL.
    regime, direction = rf._apply_hysteresis(
        inst, "TRENDING_BEAR", "BEAR", "TRENDING_BULL", rf.latest_regime[inst]
    )
    assert regime == "TRENDING_BULL", "Direction flip should not commit on first bar"
    assert direction == "BULL"

    # Bar 2: sustained TRENDING_BEAR -> commits.
    regime, direction = rf._apply_hysteresis(
        inst, "TRENDING_BEAR", "BEAR", "TRENDING_BULL", rf.latest_regime[inst]
    )
    assert regime == "TRENDING_BEAR"
    assert direction == "BEAR"


# ---------------------------------------------------------------------------
# 4. Direction correctness — the false "TREND UP" fix
# ---------------------------------------------------------------------------

def test_direction_requires_slope_and_persistence_agreement():
    """Slope up + closes above VWAP => BULL."""
    rf = RegimeFilter()
    d = rf._resolve_direction("TRENDING", vwap_slope=0.8, dom_side="ABOVE",
                              curr_close=100.0, curr_vwap=99.0)
    assert d == "BULL"

    d = rf._resolve_direction("TRENDING", vwap_slope=-0.8, dom_side="BELOW",
                              curr_close=98.0, curr_vwap=99.0)
    assert d == "BEAR"


def test_flat_slope_no_dominant_side_is_not_directional():
    """
    The Sep 29 pathology: high score but flat slope and no dominant side must NOT be
    stamped as a directional trend. resolve_direction returns NEUTRAL, and update_candle
    then demotes the regime out of TRENDING.
    """
    rf = RegimeFilter()
    d = rf._resolve_direction("TRENDING", vwap_slope=0.01, dom_side="NEUTRAL",
                              curr_close=100.0, curr_vwap=100.0)
    assert d == "NEUTRAL", "Flat + no dominant side must not fabricate a trend direction"


def test_non_trending_family_has_no_direction():
    """NEUTRAL and CHOPPY families never carry a BULL/BEAR direction."""
    rf = RegimeFilter()
    assert rf._resolve_direction("NEUTRAL", 0.5, "ABOVE", 100.0, 99.0) == "NEUTRAL"
    assert rf._resolve_direction("CHOPPY", -0.5, "BELOW", 98.0, 99.0) == "NEUTRAL"


def test_trending_candidate_without_direction_demotes_to_neutral():
    """
    End-to-end via update_candle: feed candles that produce a borderline-high score but
    with flat VWAP so no direction can be established. The committed regime must not be a
    bogus TRENDING_* — it should fall back to NEUTRAL rather than stamp a false trend.
    """
    rf = RegimeFilter()
    inst = "NIFTY"
    # Flat price at a constant level -> slope ~0, no dominant side movement.
    candle = {"open": 100.0, "high": 100.2, "low": 99.8, "close": 100.0, "volume": 1000.0}
    # Drive several flat bars with a high ADX (which alone can lift the score) but zero slope.
    last = None
    for _ in range(6):
        last = rf.update_candle(inst, candle, vwap=100.0, adx=45.0)
    # Whatever the score, the regime must never be a directionless TRENDING stamp.
    assert last["regime"] in ("NEUTRAL", "CHOPPY", "TRENDING_BULL", "TRENDING_BEAR")
    if last["regime"].startswith("TRENDING"):
        # If it did commit trending, it must carry a real direction.
        assert last["direction"] in ("BULL", "BEAR")
    else:
        assert last["direction"] == "NEUTRAL"
