import pytest
import asyncio
from datetime import datetime, timezone
from backend.core.redis_bus import RedisBus
from backend.risk.risk_governor import RiskGovernor
from backend.risk.signal_tracker import SignalTracker

@pytest.mark.asyncio
async def test_signal_tracker_lifecycle():
    redis_bus = RedisBus()
    resolved_events = []
    async def mock_publish(payload, *args, **kwargs):
        resolved_events.append(payload)
    redis_bus.publish_signal = mock_publish
    redis_bus.publish_tick = mock_publish

    gov = RiskGovernor()
    tracker = SignalTracker(redis_bus, gov)

    # 1. Test Adaptive Runaway Guardrail with default profile (+3.5% / 30s)
    sig1 = {
        "signal_id": "TEST-SIG-CHASE",
        "instrument": "NIFTY",
        "strategy": "ORB_BREAKOUT",  # Uses default 3.5% threshold
        "direction": "CE",
        "option_type": "CE",
        "option_token": "50001",
        "option_symbol": "NIFTY25000CE",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "target": 120.0,
        "quantity": 65,
        "lot_size": 65,
        "status": "ACTIVE"
    }
    tracker.register_signal(sig1)
    assert "TEST-SIG-CHASE" in tracker.active_signals

    # Tick at 104.0 (+4.0%) within 5 seconds -> Should trip default runaway guardrail (3.5%)
    await tracker.on_tick({"token": "50001", "ltp": 104.0})
    # Signal stays in active_signals but status should be INVALID_CHASE_PREVENTED
    assert tracker.active_signals["TEST-SIG-CHASE"]["status"] == "INVALID_CHASE_PREVENTED"
    assert len(resolved_events) == 1
    assert resolved_events[0]["signal"]["status"] == "INVALID_CHASE_PREVENTED"
    assert resolved_events[0]["signal"]["theoretical_pnl"] == 0.0

    # 1b. Test GAMMA_SCALP with wider threshold (+8.0%) — 5% surge should NOT trip
    sig1b = {
        "signal_id": "TEST-SIG-GAMMA-OK",
        "instrument": "NIFTY",
        "strategy": "GAMMA_SCALP",  # Uses 8.0% threshold
        "direction": "CE",
        "option_type": "CE",
        "option_token": "50011",
        "option_symbol": "NIFTY23400CE",
        "entry_price": 30.0,
        "stop_loss": 24.0,
        "target": 42.0,
        "quantity": 130,
        "lot_size": 65,
        "status": "ACTIVE"
    }
    tracker.register_signal(sig1b)
    # Tick at 31.5 (+5.0%) — should NOT trip GAMMA_SCALP's 8.0% threshold
    await tracker.on_tick({"token": "50011", "ltp": 31.5})
    assert tracker.active_signals["TEST-SIG-GAMMA-OK"]["status"] == "ACTIVE"

    # 2. Test Target Hit (> 30s elapsed)
    sig2 = {
        "signal_id": "TEST-SIG-TARGET",
        "instrument": "NIFTY",
        "strategy": "VWAP_EMA",
        "direction": "CE",
        "option_type": "CE",
        "option_token": "50002",
        "option_symbol": "NIFTY25000CE",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "target": 120.0,
        "quantity": 65,
        "lot_size": 65,
        "status": "ACTIVE"
    }
    tracker.register_signal(sig2)
    # Simulate 35 seconds elapsed
    tracker.active_signals["TEST-SIG-TARGET"]["registered_ts"] = datetime.now(timezone.utc).timestamp() - 35

    # Tick at 120.5 (Target Hit)
    await tracker.on_tick({"token": "50002", "ltp": 120.5})
    assert tracker.active_signals["TEST-SIG-TARGET"]["status"] == "TARGET_HIT"
    assert resolved_events[-1]["signal"]["status"] == "TARGET_HIT"
    assert resolved_events[-1]["signal"]["theoretical_pnl"] == (120.0 - 100.0) * 65 # +1300.0

    # 3. Test Stop Hit
    sig3 = {
        "signal_id": "TEST-SIG-STOP",
        "instrument": "NIFTY",
        "strategy": "VWAP_EMA",
        "direction": "CE",
        "option_type": "CE",
        "option_token": "50003",
        "option_symbol": "NIFTY25000CE",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "target": 120.0,
        "quantity": 65,
        "lot_size": 65,
        "status": "ACTIVE"
    }
    tracker.register_signal(sig3)
    # Tick at 89.5 (Stop Hit)
    await tracker.on_tick({"token": "50003", "ltp": 89.5})
    assert tracker.active_signals["TEST-SIG-STOP"]["status"] == "STOP_HIT"
    assert resolved_events[-1]["signal"]["status"] == "STOP_HIT"
    assert resolved_events[-1]["signal"]["theoretical_pnl"] == (90.0 - 100.0) * 65 # -650.0


@pytest.mark.asyncio
async def test_resolution_callback_fired():
    """Verify that resolution callbacks are called when signals resolve."""
    redis_bus = RedisBus()
    async def mock_publish(payload, *args, **kwargs):
        pass
    redis_bus.publish_signal = mock_publish
    redis_bus.publish_tick = mock_publish

    gov = RiskGovernor()
    tracker = SignalTracker(redis_bus, gov)

    callback_signals = []
    def test_callback(sig):
        callback_signals.append(sig)

    tracker.register_resolution_callback(test_callback)

    sig = {
        "signal_id": "TEST-CB",
        "instrument": "NIFTY",
        "strategy": "VWAP_EMA",
        "direction": "PE",
        "option_type": "PE",
        "option_token": "60001",
        "option_symbol": "NIFTY23300PE",
        "entry_price": 50.0,
        "stop_loss": 40.0,
        "target": 70.0,
        "quantity": 65,
        "lot_size": 65,
        "status": "ACTIVE"
    }
    tracker.register_signal(sig)
    await tracker.on_tick({"token": "60001", "ltp": 39.0})  # Stop hit

    assert len(callback_signals) == 1
    assert callback_signals[0]["status"] == "STOP_HIT"
