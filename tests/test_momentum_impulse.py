import pytest
import asyncio
from datetime import datetime, timezone
from backend.strategies.momentum_impulse import MomentumImpulseDetector
from backend.risk.risk_governor import RiskGovernor


@pytest.mark.asyncio
async def test_momentum_impulse_lifecycle():
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

    strat = MomentumImpulseDetector(config)
    gov = RiskGovernor()

    base_time = 1727255000.0  # arbitrary epoch timestamp (14:33 IST)
    # Pre-seed spot
    strat.seed_from_spot("NIFTY", {"ltp": 25000.0})

    # 1. Simulate moderate spot move (less than radar threshold ~ 16.25 pts)
    # Send ticks: +10 pts in 5s
    spot_meta = {"is_spot": True, "name": "NIFTY"}
    for i, pts in enumerate([25000.0, 25003.0, 25006.0, 25010.0]):
        res = await strat.on_tick({
            "token": "99926000",
            "ltp": pts,
            "exchange_timestamp": base_time + i * 2.0
        }, meta=spot_meta)
        assert res is None

    assert len(strat.pending_alerts) == 0
    assert strat.active_impulses["NIFTY"] is None

    # 2. Push spot velocity over Radar threshold (+18 pts total in 10s)
    # 25018.0 - 25000.0 = +18.0 pts >= 16.25 pts (65% of 25.0)
    await strat.on_tick({
        "token": "99926000",
        "ltp": 25018.0,
        "exchange_timestamp": base_time + 10.0
    }, meta=spot_meta)

    # Radar pre-alert should be emitted
    assert len(strat.pending_alerts) == 1
    alert = strat.pending_alerts.popleft()
    assert alert["alert_type"] == "MOMENTUM_IMPULSE"
    assert alert["instrument"] == "NIFTY"
    assert alert["direction"] == "CE"
    assert "⚡ NIFTY Momentum Impulse Detected" in alert["title"]

    # 3. Push spot velocity to Full Impulse threshold (+28 pts total in 14s)
    await strat.on_tick({
        "token": "99926000",
        "ltp": 25028.0,
        "exchange_timestamp": base_time + 14.0
    }, meta=spot_meta)

    assert strat.active_impulses["NIFTY"] is not None
    impulse = strat.active_impulses["NIFTY"]
    assert impulse["direction"] == "CE"
    assert impulse["spot_curr"] == 25028.0
    assert impulse["spot_delta"] >= 25.0

    # 4. Now simulate ATM CE Option ticks
    opt_meta = {
        "name": "NIFTY",
        "option_type": "CE",
        "strike": 25000.0,
        "offset": 0,
        "lot_size": 65,
        "symbol": "NIFTY25000CE"
    }

    # Baseline option tick at 100.0 (before surge)
    await strat.on_tick({
        "token": "100001",
        "ltp": 100.0,
        "exchange_timestamp": base_time + 10.0
    }, meta=opt_meta)

    # Tick at 102.0 (+2.0% surge) -> Below min_opt_surge_pct (3.5%), should NOT trigger yet
    res = await strat.on_tick({
        "token": "100001",
        "ltp": 102.0,
        "exchange_timestamp": base_time + 14.5
    }, meta=opt_meta)
    assert res is None

    # Tick at 106.0 (+6.0% surge) -> Between 3.5% and 16.0% -> SHOULD TRIGGER!
    candidate = await strat.on_tick({
        "token": "100001",
        "ltp": 106.0,
        "exchange_timestamp": base_time + 15.0
    }, meta=opt_meta)

    assert candidate is not None
    assert candidate["strategy"] == "MOMENTUM_IMPULSE"
    assert candidate["instrument"] == "NIFTY"
    assert candidate["direction"] == "CE"
    assert candidate["entry_price"] == 106.0
    assert candidate["spot_entry"] == 25028.0
    assert candidate["confidence"] >= 80
    assert candidate["custom_sl_spot"] is not None
    assert candidate["custom_sl_spot"] < 25000.0  # Anchored below impulse start

    # Impulse state should now be consumed
    assert strat.active_impulses["NIFTY"] is None

    # Evaluate candidate with RiskGovernor
    evaluated = gov.evaluate_signal(candidate)
    assert evaluated is not None
    assert evaluated["status"] == "ACTIVE"
    assert evaluated["stop_loss"] < 106.0
    assert evaluated["target"] > 106.0
    assert evaluated["risk_amount"] > 0
    assert evaluated["target_1r"] > 106.0
    assert evaluated["target_2r"] > evaluated["target_1r"]


@pytest.mark.asyncio
async def test_momentum_impulse_anti_top_chasing_guard():
    """Verify that if option premium has already surged > 16%, the signal is rejected to avoid buying top."""
    config = {
        "start_time": "09:15:00",
        "end_time": "15:30:00",
        "nifty_velocity_pts": 25.0,
        "window_sec": 20.0,
        "min_tick_count": 4,
        "min_directional_pct": 0.70,
        "min_opt_surge_pct": 3.5,
        "max_opt_surge_pct": 16.0,
        "impulse_ttl_sec": 25.0,
        "cooldown_sec": 60.0
    }
    strat = MomentumImpulseDetector(config)
    base_time = 1727255000.0
    spot_meta = {"is_spot": True, "name": "NIFTY"}

    # Establish rapid +30 pt impulse
    for i, pts in enumerate([25000.0, 25008.0, 25018.0, 25030.0]):
        await strat.on_tick({
            "token": "99926000",
            "ltp": pts,
            "exchange_timestamp": base_time + i * 3.0
        }, meta=spot_meta)

    assert strat.active_impulses["NIFTY"] is not None

    opt_meta = {
        "name": "NIFTY",
        "option_type": "CE",
        "strike": 25000.0,
        "offset": 0,
        "lot_size": 65,
        "symbol": "NIFTY25000CE"
    }

    # Baseline option at 100.0
    await strat.on_tick({
        "token": "100001",
        "ltp": 100.0,
        "exchange_timestamp": base_time + 5.0
    }, meta=opt_meta)

    # Option surges to 122.0 (+22.0% surge!) -> Exceeds max_opt_surge_pct (16.0%)
    res = await strat.on_tick({
        "token": "100001",
        "ltp": 122.0,
        "exchange_timestamp": base_time + 12.0
    }, meta=opt_meta)

    # Signal MUST be rejected (anti-top chasing / staleness protection)
    assert res is None


@pytest.mark.asyncio
async def test_momentum_impulse_pe_breakdown():
    """Verify PE downside impulse detection on sharp market selloff."""
    config = {
        "start_time": "09:15:00",
        "end_time": "15:30:00",
        "nifty_velocity_pts": 25.0,
        "window_sec": 20.0,
        "min_tick_count": 4,
        "min_directional_pct": 0.70,
        "min_opt_surge_pct": 3.5,
        "max_opt_surge_pct": 16.0,
        "impulse_ttl_sec": 25.0
    }
    strat = MomentumImpulseDetector(config)
    base_time = 1727255000.0
    spot_meta = {"is_spot": True, "name": "NIFTY"}

    # Rapid selloff: 25000 -> 24968 (-32 pts in 12s)
    for i, pts in enumerate([25000.0, 24990.0, 24980.0, 24968.0]):
        await strat.on_tick({
            "token": "99926000",
            "ltp": pts,
            "exchange_timestamp": base_time + i * 3.0
        }, meta=spot_meta)

    assert strat.active_impulses["NIFTY"] is not None
    assert strat.active_impulses["NIFTY"]["direction"] == "PE"

    opt_meta = {
        "name": "NIFTY",
        "option_type": "PE",
        "strike": 25000.0,
        "offset": 0,
        "lot_size": 65,
        "symbol": "NIFTY25000PE"
    }

    # Baseline put option at 80.0
    await strat.on_tick({
        "token": "100002",
        "ltp": 80.0,
        "exchange_timestamp": base_time + 4.0
    }, meta=opt_meta)

    # Put option expands to 85.0 (+6.25% expansion) -> Should trigger PE signal
    candidate = await strat.on_tick({
        "token": "100002",
        "ltp": 85.0,
        "exchange_timestamp": base_time + 12.5
    }, meta=opt_meta)

    assert candidate is not None
    assert candidate["direction"] == "PE"
    assert candidate["option_type"] == "PE"
    assert candidate["entry_price"] == 85.0
    assert candidate["custom_sl_spot"] > 25000.0  # Above breakdown origin
