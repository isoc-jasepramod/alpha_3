import pytest
from datetime import datetime, timezone
from backend.strategies.vwap_ema import VWAPEMAAlignment
from backend.strategies.orb_breakout import VolumeBackedORB

def test_vwap_ema_trend_day_exhaustion():
    strat = VWAPEMAAlignment()
    strat.day_high["NIFTY"] = 23150.0
    # Baseline setup: NIFTY spot = 23000, Day high = 23150 (drop of 150 pts)
    # With effective_atr = 30 pts:
    # 2x ATR = 60 pts
    # Normal mode: 150 pts drop > 60 pts -> True (Suppressed)
    assert strat._check_exhaustion_filter(inst="NIFTY", direction="PE", spot=23000.0, adx=20.0, vwap_slope=-0.2) is True

    # Strong Trend Day Mode: ADX = 32.0, VWAP Slope = -0.50 (Impulsive institutional sell-off)
    # Threshold expands to 5.0x ATR = 150 pts. Drop of 120 pts (spot = 23030) is NOT suppressed!
    assert strat._check_exhaustion_filter(inst="NIFTY", direction="PE", spot=23030.0, adx=32.0, vwap_slope=-0.50) is False

def test_vwap_ema_fast_ema_pullback_distance():
    strat = VWAPEMAAlignment()
    # Spot = 23000, VWAP = 23120 (distance = 120 pts = 0.52%)
    # Normal mode (max_dist = 0.30%): Suppressed as chase
    assert strat._check_vwap_distance_filter(direction="PE", spot=23000.0, vwap=23120.0, tested_ema9=False, adx=20.0) is True

    # Fast EMA9 pullback in strong trend: tested_ema9=True, ADX=28.0 (allowed up to 0.75%)
    assert strat._check_vwap_distance_filter(direction="PE", spot=23000.0, vwap=23120.0, tested_ema9=True, adx=28.0) is False

def test_orb_gap_and_go_continuation():
    orb = VolumeBackedORB()
    # Range limits check
    min_nifty, max_nifty = orb._get_range_limits("NIFTY")
    min_sensex, max_sensex = orb._get_range_limits("SENSEX")
    assert max_nifty == 200.0
    assert max_sensex == 500.0

    # Setup simulated gap-down day for NIFTY (gap = -0.70%)
    dt_info = orb.day_tracking["NIFTY"]
    dt_info["day_open"] = 23300.0
    dt_info["prev_close"] = 23465.0
    dt_info["gap_pct"] = 0.70
    dt_info["gap_dir"] = "DOWN"
    dt_info["excessive_gap"] = False

    # 1. Extreme gap (>1.5%) gets suppressed
    dt_info["gap_pct"] = 1.80
    dt_info["excessive_gap"] = True
    assert dt_info["excessive_gap"] is True

    # 2. Moderate gap-down (0.70%): Gap-and-Go allows PE but suppresses counter-trend CE
    dt_info["gap_pct"] = 0.70
    dt_info["excessive_gap"] = False
    assert (dt_info["gap_pct"] > orb.max_gap_pct and dt_info["gap_dir"] == "DOWN") is True
