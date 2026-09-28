import pytest
from datetime import datetime, timezone
from backend.strategies.gamma_scalp import ExpiryDayGammaScalp

@pytest.mark.asyncio
async def test_gamma_scalp_incident_prevents_ce_pe_spam():
    """
    Direct regression test for Monday 28 Sep incident:
    In a 90-pt SENSEX consolidation (80000 - 80090), price ping-pongs between
    top and bottom boundaries.
    Asserts:
    1. Only ONE clean GAMMA_ARMED alert is emitted.
    2. Alternating CE and PE alerts within seconds are strictly blocked by the 5-min rate limit & directional lock.
    3. Alert is honest: tagged WATCH, is_trade=False, contains 'NOT A TRADE', and has no 'bias' or 'prepare to execute'.
    """
    config = {
        "start_time": "13:15:00",
        "end_time": "15:30:00",
        "consolidation_start": "12:00:00",
        "consolidation_end": "13:00:00",
        "watch_proximity_pct": 0.08,
        "min_approach_velocity_pct": 0.04,
        "armed_cooldown_sec": 300.0,
        "armed_ttl_sec": 300.0,
        "enforce_expiry_day": False
    }

    strat = ExpiryDayGammaScalp(config)
    inst = "SENSEX"

    # Midday consolidation range: 80000 to 80090 (90 pt range on SENSEX)
    strat.mid_ranges[inst] = {
        "high": 80090.0,
        "low": 80000.0,
        "finalized": True
    }

    # Monday 28 Sep 2026, 13:20:00 IST -> 07:50:00 UTC
    # Epoch timestamp: 1790668200
    base_ts = 1790668200.0
    spot_meta = {"is_spot": True, "name": inst}

    # 1. Approach Range Top (H_mid = 80090) from below:
    # 80040 -> 80055 -> 80075 -> 80080 in 15 seconds (+0.05% velocity, within 0.08% of 80090)
    for i, p in enumerate([80040.0, 80055.0, 80070.0, 80080.0]):
        await strat.on_tick({
            "token": "99919000",
            "ltp": p,
            "exchange_timestamp": base_ts + i * 5.0
        }, meta=spot_meta)

    # First GAMMA_ARMED alert for CE MUST fire
    assert len(strat.pending_alerts) == 1
    first_alert = strat.pending_alerts.popleft()
    assert first_alert["alert_type"] == "GAMMA_ARMED"
    assert first_alert["direction"] == "CE"
    assert first_alert["tier"] == "WATCH"
    assert first_alert["is_trade"] is False
    assert "NOT A TRADE" in first_alert["message"]
    # Verify honest framing: no 'bias', no 'prepare to execute', no 'breakout imminent'
    assert "bias" not in first_alert["title"].lower()
    assert "prepare to execute" not in first_alert["message"].lower()
    assert "breakout imminent" not in first_alert["message"].lower()

    # 2. 3 SECONDS LATER (The exact incident scenario):
    # Spot sharply rejects the top wall and plunges to 80008 (testing L_mid = 80000)
    t_3s = base_ts + 18.0
    strat.spot_tick_history[inst].clear()
    strat.spot_tick_history[inst].append((t_3s - 10.0, 80060.0))
    await strat.on_tick({
        "token": "99919000",
        "ltp": 80008.0,
        "exchange_timestamp": t_3s
    }, meta=spot_meta)

    # MUST NOT emit PE alert 3 seconds later! Directional lock & 5-min cooldown blocks it
    assert len(strat.pending_alerts) == 0, "PE alert fired 3 seconds after CE alert! Directional lock failed."

    # 3. 15 seconds later: Price bounces back towards the top wall (80085)
    t_15s = base_ts + 30.0
    strat.spot_tick_history[inst].clear()
    strat.spot_tick_history[inst].append((t_15s - 10.0, 80020.0))
    await strat.on_tick({
        "token": "99919000",
        "ltp": 80085.0,
        "exchange_timestamp": t_15s
    }, meta=spot_meta)

    # MUST NOT emit another CE alert! 5-min cooldown blocks it
    assert len(strat.pending_alerts) == 0, "Duplicate CE alert fired within 5-min cooldown!"

    # 4. Simulate continuous ping-ponging across 20 ticks over the next 2 minutes
    for tick_i in range(20):
        osc_p = 80085.0 if (tick_i % 2 == 0) else 80005.0
        await strat.on_tick({
            "token": "99919000",
            "ltp": osc_p,
            "exchange_timestamp": base_ts + 40.0 + tick_i * 5.0
        }, meta=spot_meta)

    # Still exactly ZERO new alerts during the range consolidation!
    assert len(strat.pending_alerts) == 0, "Spam alerts emitted during range consolidation!"

@pytest.mark.asyncio
async def test_gamma_scalp_two_stage_linkage_and_unlinked_suppression():
    """
    Asserts:
    1. When breakout occurs after GAMMA_ARMED, the CONFIRMED signal contains 'linked_alert_id'.
    2. When breakout occurs without a preceding active GAMMA_ARMED alert, the signal is SUPPRESSED.
    """
    config = {
        "start_time": "13:15:00",
        "end_time": "15:30:00",
        "consolidation_start": "12:00:00",
        "consolidation_end": "13:00:00",
        "watch_proximity_pct": 0.08,
        "min_approach_velocity_pct": 0.04,
        "enforce_expiry_day": False
    }

    strat = ExpiryDayGammaScalp(config)
    inst = "SENSEX"
    strat.mid_ranges[inst] = {"high": 80090.0, "low": 80000.0, "finalized": True}

    base_ts = 1790668200.0
    spot_meta = {"is_spot": True, "name": inst}

    # Case A: Breakout WITHOUT prior GAMMA_ARMED alert
    # Spot moves above 80090 directly
    strat.spot_tick_history[inst].append((base_ts - 20, 80000.0))
    strat.spot_tick_history[inst].append((base_ts, 80110.0)) # +0.13% velocity breakout

    opt_meta = {
        "is_spot": False,
        "name": inst,
        "symbol": "SENSEX 80100 CE",
        "option_type": "CE",
        "strike": 80100.0,
        "lot_size": 10
    }
    opt_tick = {
        "token": "7001",
        "ltp": 95.0, # Within SENSEX 60-150 range
        "volume": 5000.0,
        "exchange_timestamp": base_ts + 1.0
    }

    # Unlinked breakout MUST be suppressed
    unlinked_sig = await strat.on_tick(opt_tick, meta=opt_meta)
    assert unlinked_sig is None, "Breakout triggered without preceding GAMMA_ARMED alert!"

    # Case B: Arm setup first via honest pre-alert approach
    strat.spot_tick_history[inst].clear()
    strat.spot_tick_history[inst].append((base_ts + 2.0, 80040.0))
    await strat.on_tick({
        "token": "99919000",
        "ltp": 80080.0,
        "exchange_timestamp": base_ts + 15.0
    }, meta=spot_meta)

    assert len(strat.pending_alerts) == 1
    armed_alert = strat.pending_alerts.popleft()
    assert armed_alert["alert_type"] == "GAMMA_ARMED"
    assert strat.armed_setups[inst] is not None
    assert strat.armed_setups[inst]["alert_id"] == armed_alert["id"]

    # Now deliver the true breakout
    strat.spot_tick_history[inst].append((base_ts + 20.0, 80110.0))
    strat.spot_3m_history[inst].append((base_ts, 80000.0))
    strat.spot_3m_history[inst].append((base_ts + 20.0, 80110.0))

    confirmed_sig = await strat.on_tick({
        "token": "7001",
        "ltp": 98.0,
        "volume": 8000.0,
        "exchange_timestamp": base_ts + 21.0
    }, meta=opt_meta)

    assert confirmed_sig is not None, "Confirmed signal failed to trigger after armed alert!"
    assert confirmed_sig["tier"] == "ACTIONABLE"
    assert confirmed_sig["is_trade"] is True
    assert confirmed_sig["details"]["linked_alert_id"] == armed_alert["id"]
    # Armed state must now be cleared
    assert strat.armed_setups[inst] is None
