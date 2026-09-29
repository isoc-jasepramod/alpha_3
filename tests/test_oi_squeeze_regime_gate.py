"""
PROJECT ALPHA 3.0 — OI SQUEEZE REGIME GATE TESTS

Sep 29 recorded-tick backtest showed OI Squeeze bled on counter-trend CE spikes on a
bearish/choppy day. These tests verify the regime gate:
  - CHOPPY: both directions suppressed.
  - TRENDING_BULL: CE allowed, PE (counter-trend) blocked.
  - TRENDING_BEAR: PE allowed, CE (counter-trend) blocked.
  - NEUTRAL: both allowed (directional gate only bites in TRENDING).
"""
import pytest
from backend.strategies.regime_filter import RegimeFilter


def _set(rf, inst, regime):
    rf.latest_regime[inst] = {
        "regime": regime, "score": 70.0 if "TRENDING" in regime else (50.0 if regime == "NEUTRAL" else 35.0),
        "is_trending": "TRENDING" in regime, "is_neutral": regime == "NEUTRAL",
        "is_choppy": regime == "CHOPPY",
    }


def test_choppy_suppresses_both_directions():
    rf = RegimeFilter()
    _set(rf, "NIFTY", "CHOPPY")
    assert rf.allows_oi_squeeze("NIFTY", "CE")[0] is False
    assert rf.allows_oi_squeeze("NIFTY", "PE")[0] is False


def test_trending_bull_allows_ce_blocks_pe():
    rf = RegimeFilter()
    _set(rf, "NIFTY", "TRENDING_BULL")
    assert rf.allows_oi_squeeze("NIFTY", "CE")[0] is True
    assert rf.allows_oi_squeeze("NIFTY", "PE")[0] is False


def test_trending_bear_allows_pe_blocks_ce():
    rf = RegimeFilter()
    _set(rf, "NIFTY", "TRENDING_BEAR")
    assert rf.allows_oi_squeeze("NIFTY", "PE")[0] is True
    assert rf.allows_oi_squeeze("NIFTY", "CE")[0] is False


def test_neutral_allows_both():
    rf = RegimeFilter()
    _set(rf, "NIFTY", "NEUTRAL")
    assert rf.allows_oi_squeeze("NIFTY", "CE")[0] is True
    assert rf.allows_oi_squeeze("NIFTY", "PE")[0] is True


def test_disabled_filter_allows_all():
    rf = RegimeFilter({"enabled": False})
    assert rf.allows_oi_squeeze("NIFTY", "CE")[0] is True
    assert rf.allows_oi_squeeze("NIFTY", "PE")[0] is True
