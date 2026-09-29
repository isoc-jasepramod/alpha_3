"""
PROJECT ALPHA 3.0 — MOMENTUM PULLBACK & ACCELERATION TESTS

The Sep 29 radar->signal gap was ~1 second, so a human could not act on the vertical
spike. These tests cover the two mitigations:
  1. MOMENTUM_ACCELERATION: an earliest heads-up when velocity is rising (2nd derivative).
  2. Post-impulse PULLBACK entry: after arming we wait for a shallow dip that HOLDS above
     the impulse origin, then signal a human-takeable entry (not the vertical itself).
"""

import pytest
from backend.strategies.momentum_impulse import MomentumImpulseDetector

NIFTY_SPOT_TOKEN = "99926000"

BASE_CONFIG = {
    "start_time": "09:15:00",
    "end_time": "15:30:00",
    "nifty_velocity_pts": 25.0,
    "sensex_velocity_pts": 75.0,
    "window_sec": 20.0,
    "min_tick_count": 4,
    "min_directional_pct": 0.70,
    "radar_velocity_ratio": 0.50,
    "min_opt_surge_pct": 3.5,
    "max_opt_surge_pct": 16.0,
    "impulse_ttl_sec": 25.0,
    "cooldown_sec": 90.0,
    "accel_enabled": True,
    "nifty_accel_min_recent_vel": 2.5,
    "accel_recent_window_sec": 5.0,
    "accel_ratio": 1.6,
    "pullback_enabled": True,
    "pullback_max_retrace": 0.50,
    "pullback_resume_frac": 0.20,
    "pullback_window_sec": 90.0,
}

# base unix ts within session hours (IST). 1727254800 = 2026-09-25 ~09:30 IST region.
BASE_TS = 1727254800.0
SPOT_META = {"is_spot": True, "name": "NIFTY"}


async def _feed_spot(strat, prices_with_ts):
    for (p, t) in prices_with_ts:
        await strat.on_tick({"token": NIFTY_SPOT_TOKEN, "ltp": p, "exchange_timestamp": t}, meta=SPOT_META)


@pytest.mark.asyncio
async def test_acceleration_prealert_fires_when_velocity_rises():
    strat = MomentumImpulseDetector(BASE_CONFIG)
    strat.seed_from_spot("NIFTY", {"ltp": 25000.0})

    # Older sub-window (0-5s): slow drift +1 pt/s. Recent sub-window (5-10s): fast +4 pt/s.
    ticks = [
        (25000.0, BASE_TS + 0.0),
        (25001.0, BASE_TS + 1.0),
        (25002.0, BASE_TS + 2.0),
        (25003.0, BASE_TS + 3.0),
        (25004.0, BASE_TS + 4.0),
        (25008.0, BASE_TS + 5.0),
        (25012.0, BASE_TS + 6.0),
        (25016.0, BASE_TS + 7.0),
        (25020.0, BASE_TS + 8.0),
    ]
    await _feed_spot(strat, ticks)

    accel_alerts = [a for a in strat.pending_alerts if a["alert_type"] == "MOMENTUM_ACCELERATION"]
    assert len(accel_alerts) >= 1
    assert accel_alerts[0]["category"] == "WATCH"
    assert accel_alerts[0]["direction"] == "CE"


@pytest.mark.asyncio
async def test_pullback_mode_does_not_signal_on_the_vertical():
    """
    In pullback mode, the strategy must ARM on the impulse but must NOT emit a signal on the
    first option surge while still AWAITING_PULLBACK (that would be chasing the spike).
    """
    strat = MomentumImpulseDetector(BASE_CONFIG)
    strat.seed_from_spot("NIFTY", {"ltp": 25000.0})

    # Vertical impulse: +30 pts fast -> arms (full threshold 25).
    impulse = [
        (25000.0, BASE_TS + 0.0),
        (25008.0, BASE_TS + 1.0),
        (25016.0, BASE_TS + 2.0),
        (25024.0, BASE_TS + 3.0),
        (25030.0, BASE_TS + 4.0),
    ]
    await _feed_spot(strat, impulse)

    imp = strat.active_impulses["NIFTY"]
    assert imp is not None, "impulse should arm"
    assert imp["phase"] == "AWAITING_PULLBACK"

    # Option surges right now (chasing the top) — must NOT signal yet.
    opt_meta = {"name": "NIFTY", "option_type": "CE", "strike": 25000.0, "lot_size": 50,
                "symbol": "NIFTY25000CE", "offset": 0}
    # seed a base then a surge
    await strat.on_tick({"token": "OPT1", "ltp": 100.0, "exchange_timestamp": BASE_TS + 4.2}, meta=opt_meta)
    sig = await strat.on_tick({"token": "OPT1", "ltp": 110.0, "exchange_timestamp": BASE_TS + 4.5}, meta=opt_meta)
    assert sig is None, "must not fire while AWAITING_PULLBACK"


@pytest.mark.asyncio
async def test_pullback_entry_signals_after_dip_holds():
    """Full flow: arm -> shallow pullback -> resume/hold -> PULLBACK_READY -> option surge -> signal."""
    strat = MomentumImpulseDetector(BASE_CONFIG)
    strat.seed_from_spot("NIFTY", {"ltp": 25000.0})

    # 1. Impulse +30 pts (origin 25000, peak 25030). Impulse size = 30.
    await _feed_spot(strat, [
        (25000.0, BASE_TS + 0.0),
        (25008.0, BASE_TS + 1.0),
        (25016.0, BASE_TS + 2.0),
        (25024.0, BASE_TS + 3.0),
        (25030.0, BASE_TS + 4.0),
    ])
    assert strat.active_impulses["NIFTY"]["phase"] == "AWAITING_PULLBACK"

    # 2. Shallow pullback to 25020 (retrace 10 pts = 33% of 30, within 50% max),
    #    then resume up to 25027 (recover 7 pts >= 20% of 30 = 6 pts) -> holds.
    await _feed_spot(strat, [
        (25026.0, BASE_TS + 6.0),
        (25022.0, BASE_TS + 8.0),
        (25020.0, BASE_TS + 10.0),   # pullback low
        (25024.0, BASE_TS + 12.0),
        (25027.0, BASE_TS + 14.0),   # resume >= 6 pts off the low
    ])

    imp = strat.active_impulses["NIFTY"]
    assert imp is not None, "impulse should still be alive"
    assert imp["phase"] == "PULLBACK_READY", f"expected PULLBACK_READY, got {imp['phase']}"

    ready_alerts = [a for a in strat.pending_alerts if a["alert_type"] == "MOMENTUM_PULLBACK_READY"]
    assert len(ready_alerts) == 1
    assert ready_alerts[0]["category"] == "ACTIONABLE"

    # 3. ATM option confirms with a surge -> signal fires, tagged as a PULLBACK entry.
    opt_meta = {"name": "NIFTY", "option_type": "CE", "strike": 25000.0, "lot_size": 50,
                "symbol": "NIFTY25000CE", "offset": 0}
    await strat.on_tick({"token": "OPT1", "ltp": 100.0, "exchange_timestamp": BASE_TS + 14.2}, meta=opt_meta)
    sig = await strat.on_tick({"token": "OPT1", "ltp": 106.0, "exchange_timestamp": BASE_TS + 14.6}, meta=opt_meta)

    assert sig is not None, "pullback entry should fire on option surge"
    assert sig["details"]["entry_type"] == "PULLBACK"
    # SL anchored to the pullback low (25020) minus buffer, not the far origin.
    assert sig["custom_sl_spot"] < 25020.0 and sig["custom_sl_spot"] > 25000.0


@pytest.mark.asyncio
async def test_deep_reversal_kills_impulse():
    """A retrace deeper than pullback_max_retrace invalidates the setup (no signal)."""
    strat = MomentumImpulseDetector(BASE_CONFIG)
    strat.seed_from_spot("NIFTY", {"ltp": 25000.0})

    await _feed_spot(strat, [
        (25000.0, BASE_TS + 0.0),
        (25008.0, BASE_TS + 1.0),
        (25016.0, BASE_TS + 2.0),
        (25024.0, BASE_TS + 3.0),
        (25030.0, BASE_TS + 4.0),
    ])
    assert strat.active_impulses["NIFTY"] is not None

    # Deep reversal to 25010: retrace 20 pts = 67% of 30 > 50% max -> impulse dropped.
    await _feed_spot(strat, [
        (25020.0, BASE_TS + 6.0),
        (25010.0, BASE_TS + 8.0),
    ])
    assert strat.active_impulses["NIFTY"] is None
