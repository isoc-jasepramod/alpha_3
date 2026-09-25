import pytest
from datetime import datetime, timezone
from backend.strategies.indicators import VolumeContactDetector, VolumeDryUpDetector
from backend.strategies.vwap_ema import VWAPEMAAlignment
from backend.strategies.orb_breakout import VolumeBackedORB

def test_volume_contact_detector_bullish_rejection():
    detector = VolumeContactDetector(baseline_period=5, contact_multiplier=1.8, proximity_pct=0.001)
    
    # Feed 5 baseline candles with average volume of 1000
    for _ in range(5):
        detector.update_volume(1000.0)

    # Candle tests VWAP at 25000:
    # Low dips to 24995 (within 0.1% tolerance: 25000 * 0.001 = 25 pts)
    # Long lower wick: Open 25010, Low 24995, High 25030, Close 25025
    # Bottom wick: (25010 - 24995) / (25030 - 24995) = 15 / 35 = 0.428 (>= 0.35)
    # Volume is 2200 (2.2x baseline >= 1.8x)
    contact_candle = {
        "open": 25010.0,
        "high": 25030.0,
        "low": 24995.0,
        "close": 25025.0,
        "volume": 2200.0
    }
    
    res = detector.check_contact(contact_candle, level=25000.0, level_name="VWAP", direction="CE")
    assert res["is_contact"] is True
    assert res["tested_level"] is True
    assert res["is_rejection"] is True
    assert res["vol_ratio"] == 2.2
    assert res["boost_score"] >= 10  # base 5 + rejection 5

def test_volume_contact_detector_no_touch_or_no_vol():
    detector = VolumeContactDetector(baseline_period=5, contact_multiplier=1.8, proximity_pct=0.001)
    for _ in range(5):
        detector.update_volume(1000.0)

    # High volume (2500) but price is far from level (25200 vs 25000 level)
    far_candle = {
        "open": 25200.0,
        "high": 25230.0,
        "low": 25190.0,
        "close": 25210.0,
        "volume": 2500.0
    }
    res = detector.check_contact(far_candle, level=25000.0, level_name="VWAP", direction="CE")
    assert res["is_contact"] is False
    assert res["tested_level"] is False
    assert res["boost_score"] == 0

    # Touches level (25000) but weak volume (800 < 1.8x)
    weak_vol_candle = {
        "open": 25005.0,
        "high": 25010.0,
        "low": 24998.0,
        "close": 25005.0,
        "volume": 800.0
    }
    res2 = detector.check_contact(weak_vol_candle, level=25000.0, level_name="VWAP", direction="CE")
    assert res2["is_contact"] is False
    assert res2["tested_level"] is True
    assert res2["boost_score"] == 0

def test_volume_dry_up_detector_and_ignition():
    vdu = VolumeDryUpDetector(lookback_period=10, min_dry_bars=3, dry_vol_ratio=0.70, ignition_multiplier=2.0)

    # Establish baseline candles with volume ~1000 and range ~30
    for i in range(5):
        vdu.update({"high": 25030.0, "low": 25000.0, "open": 25010.0, "close": 25020.0, "volume": 1000.0})

    # Dry up phase: 3 consecutive bars with contracting volume
    c1 = {"high": 25025.0, "low": 25005.0, "open": 25010.0, "close": 25020.0, "volume": 600.0} # dry bar 1
    c2 = {"high": 25022.0, "low": 25008.0, "open": 25012.0, "close": 25018.0, "volume": 450.0} # dry bar 2
    c3 = {"high": 25020.0, "low": 25010.0, "open": 25015.0, "close": 25016.0, "volume": 300.0} # dry bar 3

    r1 = vdu.update(c1)
    assert r1["dry_bars"] == 1
    assert r1["is_dry_up"] is False

    r2 = vdu.update(c2)
    assert r2["dry_bars"] == 2
    assert r2["is_dry_up"] is False

    r3 = vdu.update(c3)
    assert r3["dry_bars"] == 3
    assert r3["is_dry_up"] is True  # Squeeze dry-up threshold reached!

    # Ignition bar: Explosive volume (1500 vs avg dry volume of 450 -> ratio ~3.33x >= 2.0x) and wide range
    ign_candle = {
        "high": 25070.0,
        "low": 25012.0,
        "open": 25015.0,
        "close": 25065.0,
        "volume": 1500.0
    }
    r_ign = vdu.update(ign_candle)
    assert r_ign["is_ignition"] is True
    assert r_ign["ignition_ratio"] >= 2.5
    assert r_ign["boost_score"] >= 8

def test_vwap_ema_volume_bonus_integration():
    strat = VWAPEMAAlignment()
    # Test confidence computation with volume bonuses
    conditions = {
        "trend_alignment": True,       # +20
        "volume_confirmation": True,   # +25
        "momentum_strength": 0.5,      # +10
        "time_quality": True,          # +15
        "context_filter": True,        # +20
        "volume_contact_bonus": 10,    # +10
        "volume_dryup_bonus": 8        # +8
    }
    # Base = 90, with bonuses = 108 -> clamped to 100
    conf = strat.compute_confidence(conditions)
    assert conf == 100

    # Without context filter (base 70) + volume bonus (15) = 85
    cond2 = {
        "trend_alignment": True,
        "volume_confirmation": True,
        "momentum_strength": 0.5,
        "time_quality": True,
        "context_filter": False,
        "volume_contact_bonus": 10,
        "volume_dryup_bonus": 0
    }
    assert strat.compute_confidence(cond2) == 80  # 20+25+10+15+10 = 80

def test_orb_volume_detectors_initialized():
    orb = VolumeBackedORB()
    assert "NIFTY" in orb.spot_contact_detectors
    assert "SENSEX" in orb.spot_contact_detectors
    assert "NIFTY" in orb.spot_dryup_detectors
    assert "SENSEX" in orb.spot_dryup_detectors

def test_radar_alert_emission_and_cooldown():
    strat = VWAPEMAAlignment()
    # Initial emission succeeds
    alert = strat.emit_radar_alert(
        alert_type="VOLUME_CONTACT",
        instrument="NIFTY",
        direction="CE",
        title="⚡ Test Alert",
        message="Testing radar alert",
        now_ts=1000.0
    )
    assert alert is not None
    assert alert["alert_type"] == "VOLUME_CONTACT"
    assert len(strat.pending_alerts) == 1

    # Immediate second emission within cooldown (75s) returns None and does not add duplicate
    dupe = strat.emit_radar_alert(
        alert_type="VOLUME_CONTACT",
        instrument="NIFTY",
        direction="CE",
        title="⚡ Test Alert Dupe",
        message="Testing radar alert dupe",
        now_ts=1020.0
    )
    assert dupe is None
    assert len(strat.pending_alerts) == 1

    # Emission after cooldown (e.g. 1080s > 1000 + 75) succeeds
    fresh = strat.emit_radar_alert(
        alert_type="VOLUME_CONTACT",
        instrument="NIFTY",
        direction="CE",
        title="⚡ Test Alert Fresh",
        message="Testing radar alert fresh",
        now_ts=1080.0
    )
    assert fresh is not None
    assert len(strat.pending_alerts) == 2

