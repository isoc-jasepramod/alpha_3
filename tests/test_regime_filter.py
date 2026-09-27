import pytest
from datetime import datetime, timezone
from backend.strategies.regime_filter import RegimeFilter
from backend.strategies.momentum_impulse import MomentumImpulseDetector

def test_regime_filter_scoring_components():
    rf = RegimeFilter()
    inst = "NIFTY"

    # 1. Monotonic uptrend candles
    # 15 candles steadily advancing upwards from 25000 to 25150
    candles = []
    base_price = 25000.0
    for i in range(15):
        candles.append({
            "timestamp": f"2026-09-25T09:{15 + i*3:02d}:00",
            "open": base_price + i * 10,
            "high": base_price + i * 10 + 8,
            "low": base_price + i * 10 - 2,
            "close": base_price + i * 10 + 7,
            "volume": 5000.0
        })

    # Seed
    rf.seed_from_candles(inst, candles)
    st = rf.get_regime(inst)

    # Verify Kaufman Efficiency Ratio (ER should be 1.0 for monotonic line)
    closes = [c["close"] for c in candles[-10:]]
    er_val, er_score = rf.calculate_efficiency_ratio_score(closes)
    assert er_val >= 0.95
    assert er_score >= 90.0, f"Expected high ER score for straight uptrend, got {er_score}"

    # Verify VWAP Persistence
    vwaps = list(rf.vwap_history[inst])[-10:]
    pers_candles = candles[-10:]
    pers_score, dom_side, crosses = rf.calculate_vwap_persistence_score(pers_candles, vwaps)
    assert pers_score >= 80.0, f"Expected high persistence score, got {pers_score}"
    assert dom_side == "ABOVE"
    assert crosses == 0

    # Verify VWAP Slope
    slope, slope_score = rf.calculate_vwap_slope_score(vwaps[-5:], rf.nifty_target_slope)
    assert slope > 0.0
    assert slope_score > 60.0

    # Set rising ADX
    adx_score = rf.calculate_adx_score(32.0, prev_adx=27.0)
    assert adx_score >= 90.0

    # Final seeded regime should be TRENDING_BULL
    assert "TRENDING" in st["regime"]
    assert st["score"] >= 65.0

def test_regime_filter_choppy_scoring():
    rf = RegimeFilter()
    inst = "NIFTY"

    # Oscillating chop: alternating +5, -5 around 25000
    candles = []
    for i in range(15):
        is_even = (i % 2 == 0)
        c_open = 25000.0 if is_even else 25008.0
        c_close = 25008.0 if is_even else 25000.0
        candles.append({
            "timestamp": f"2026-09-25T09:{15 + i*3:02d}:00",
            "open": c_open,
            "high": 25010.0,
            "low": 24998.0,
            "close": c_close,
            "volume": 2000.0
        })

    rf.seed_from_candles(inst, candles)
    # Feed flat/low ADX
    st = rf.update_candle(inst, candles[-1], vwap=25004.0, adx=13.0)

    assert st["regime"] == "CHOPPY"
    assert st["score"] < 45.0
    assert st["is_choppy"] is True

def test_regime_gating_behavior():
    rf = RegimeFilter()
    inst = "NIFTY"

    # --- 1. TRENDING_BULL ---
    rf.latest_regime[inst] = {
        "regime": "TRENDING_BULL",
        "score": 78.0,
        "is_trending": True,
        "is_neutral": False,
        "is_choppy": False
    }
    allowed, _ = rf.allows_vwap_ema(inst, is_volume_contact=False, is_dryup_ignition=False)
    assert allowed is True
    allowed, _ = rf.allows_vwap_ema(inst, is_volume_contact=True, is_dryup_ignition=False)
    assert allowed is True
    allowed, _ = rf.allows_momentum_arming(inst)
    assert allowed is True
    allowed, _ = rf.allows_momentum_radar(inst)
    assert allowed is True

    # --- 2. NEUTRAL (45 - 64) ---
    rf.latest_regime[inst] = {
        "regime": "NEUTRAL",
        "score": 52.0,
        "is_trending": False,
        "is_neutral": True,
        "is_choppy": False
    }
    # Regular setup disallowed
    allowed, _ = rf.allows_vwap_ema(inst, is_volume_contact=False, is_dryup_ignition=False)
    assert allowed is False
    # Volume contact & Dry-up ignition allowed
    allowed, _ = rf.allows_vwap_ema(inst, is_volume_contact=True, is_dryup_ignition=False)
    assert allowed is True
    allowed, _ = rf.allows_vwap_ema(inst, is_volume_contact=False, is_dryup_ignition=True)
    assert allowed is True
    # Momentum can still arm in NEUTRAL
    allowed, _ = rf.allows_momentum_arming(inst)
    assert allowed is True
    allowed, _ = rf.allows_momentum_radar(inst)
    assert allowed is True

    # --- 3. CHOPPY (< 45) ---
    rf.latest_regime[inst] = {
        "regime": "CHOPPY",
        "score": 35.0,
        "is_trending": False,
        "is_neutral": False,
        "is_choppy": True
    }
    # Full standdown for VWAP_EMA
    allowed, _ = rf.allows_vwap_ema(inst, is_volume_contact=False, is_dryup_ignition=False)
    assert allowed is False
    allowed, _ = rf.allows_vwap_ema(inst, is_volume_contact=True, is_dryup_ignition=False)
    assert allowed is False
    allowed, _ = rf.allows_vwap_ema(inst, is_volume_contact=False, is_dryup_ignition=True)
    assert allowed is False
    # Key requirement: Momentum radar pre-alerts STILL FIRE, but arming is suppressed!
    allowed, _ = rf.allows_momentum_radar(inst)
    assert allowed is True
    allowed, _ = rf.allows_momentum_arming(inst)
    assert allowed is False

@pytest.mark.asyncio
async def test_momentum_integration_with_regime_choppy():
    """Verify MomentumImpulseDetector emits radar alert in CHOPPY, but refuses to arm."""
    rf = RegimeFilter()
    rf.latest_regime["NIFTY"] = {
        "regime": "CHOPPY",
        "score": 38.0,
        "is_trending": False,
        "is_neutral": False,
        "is_choppy": True
    }

    config = {
        "start_time": "09:15:00",
        "end_time": "15:30:00",
        "nifty_velocity_pts": 25.0,
        "sensex_velocity_pts": 75.0,
        "window_sec": 20.0,
        "min_tick_count": 4,
        "min_directional_pct": 0.70,
        "radar_velocity_ratio": 0.65,
        "min_opt_surge_pct": 3.5,
        "max_opt_surge_pct": 16.0,
        "impulse_ttl_sec": 25.0,
        "cooldown_sec": 60.0
    }

    strat = MomentumImpulseDetector(config, regime_filter=rf)
    base_time = 1727255000.0
    strat.seed_from_spot("NIFTY", {"ltp": 25000.0})

    spot_meta = {"is_spot": True, "name": "NIFTY"}
    # Send rapid 18 pt move in 5s (meets 65% radar threshold of 16.25 pts)
    for i, pts in enumerate([25000.0, 25005.0, 25012.0, 25018.0]):
        await strat.on_tick({
            "token": "99926000",
            "ltp": pts,
            "exchange_timestamp": base_time + i * 1.5
        }, meta=spot_meta)

    # Radar alert MUST fire even in CHOPPY
    assert len(strat.pending_alerts) == 1
    alert = strat.pending_alerts.popleft()
    assert alert["alert_type"] == "MOMENTUM_IMPULSE"
    assert "⚡ NIFTY Momentum Impulse Detected" in alert["title"]

    # Now deliver the full 26 pt move that would normally arm the impulse
    await strat.on_tick({
        "token": "99926000",
        "ltp": 25026.0,
        "exchange_timestamp": base_time + 6.0
    }, meta=spot_meta)

    # In CHOPPY, active impulse should NOT be armed!
    assert strat.active_impulses["NIFTY"] is None
