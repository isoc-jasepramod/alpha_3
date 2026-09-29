"""
PROJECT ALPHA 3.0 — REGIME NET-CHANGE ANCHOR TESTS

The BULL/BEAR label used to be built purely from VWAP slope + which side of VWAP price
sits, with no awareness of the day's net move — so a red day grinding up off the lows
could read TRENDING_BULL. The net-change anchor reconciles direction against the session
net move (vs previous close, else session open) and demotes a conflicting trend to NEUTRAL.
"""
import pytest
from backend.strategies.regime_filter import RegimeFilter

BASE_TS = 1790654400.0  # within an IST trading day


def _uptrend_candles(base=25000.0, n=15):
    candles = []
    for i in range(n):
        candles.append({
            "timestamp": f"2026-09-29T09:{15 + i*3:02d}:00",
            "open": base + i * 10, "high": base + i * 10 + 8,
            "low": base + i * 10 - 2, "close": base + i * 10 + 7, "volume": 5000.0,
        })
    return candles


def test_net_change_bias_computation():
    rf = RegimeFilter()
    rf.update_session_reference("NIFTY", 22700.0, BASE_TS, prev_close=22732.0)
    bias, pct = rf._net_change_bias("NIFTY")
    assert bias == "BEAR"
    assert pct < 0

    rf.update_session_reference("NIFTY", 22800.0, BASE_TS, prev_close=22732.0)
    bias, pct = rf._net_change_bias("NIFTY")
    assert bias == "BULL"
    assert pct > 0


def test_flat_day_within_deadband_is_neutral_bias():
    rf = RegimeFilter()
    # 22732 -> 22740 is +0.035%, inside the 0.10% deadband => no enforced bias.
    rf.update_session_reference("NIFTY", 22740.0, BASE_TS, prev_close=22732.0)
    bias, _ = rf._net_change_bias("NIFTY")
    assert bias == "NEUTRAL"


def test_anchor_demotes_bull_trend_on_a_red_day():
    """
    A clean up-sloping VWAP structure would normally stamp TRENDING_BULL, but if the
    session net-change is clearly negative (red day), the anchor demotes it to NEUTRAL.
    """
    rf = RegimeFilter()
    inst = "NIFTY"
    # Set a clearly red session: price well below prev close.
    rf.update_session_reference(inst, 24800.0, BASE_TS, prev_close=25200.0)  # -1.6%

    # Feed a bullish uptrend structure (rising closes above VWAP) — would be TRENDING_BULL.
    for c in _uptrend_candles(base=24700.0):
        st = rf.update_candle(inst, c, vwap=c["close"] - 5.0, adx=32.0)
        # keep the session ltp aligned to the latest close but still net-negative vs 25200
        rf.update_session_reference(inst, c["close"], BASE_TS, prev_close=25200.0)

    final = rf.get_regime(inst)
    # Even though the micro-structure is bullish, net-change is deeply red -> not BULL.
    assert final["regime"] != "TRENDING_BULL", f"anchor should block BULL on red day, got {final['regime']}"


def test_anchor_allows_bull_trend_on_a_green_day():
    """When net-change agrees (green day), a bullish structure is allowed to stamp BULL."""
    rf = RegimeFilter()
    inst = "NIFTY"
    for c in _uptrend_candles(base=25000.0):
        rf.update_session_reference(inst, c["close"], BASE_TS, prev_close=24950.0)  # green
        rf.update_candle(inst, c, vwap=c["close"] - 5.0, adx=32.0)
    final = rf.get_regime(inst)
    assert "TRENDING" in final["regime"]
    assert final["direction"] == "BULL"


def test_anchor_disabled_does_not_demote():
    rf = RegimeFilter({"net_change_anchor_enabled": False})
    inst = "NIFTY"
    rf.update_session_reference(inst, 24800.0, BASE_TS, prev_close=25200.0)  # red
    for c in _uptrend_candles(base=24700.0):
        rf.update_session_reference(inst, c["close"], BASE_TS, prev_close=25200.0)
        rf.update_candle(inst, c, vwap=c["close"] - 5.0, adx=32.0)
    final = rf.get_regime(inst)
    # With the anchor OFF, the bullish structure is allowed to stamp BULL despite the red day.
    assert final["regime"] == "TRENDING_BULL"


def test_session_reference_resets_on_new_day():
    rf = RegimeFilter()
    inst = "NIFTY"
    rf.update_session_reference(inst, 25000.0, BASE_TS, prev_close=24900.0)
    day1_open = rf._session_ref[inst]["session_open"]
    assert day1_open == 25000.0
    # Next day (+1 day in seconds): session_open should reset to the new first tick.
    rf.update_session_reference(inst, 25500.0, BASE_TS + 86400.0)
    assert rf._session_ref[inst]["session_open"] == 25500.0
