"""
TEST SUITE: AngelExecutionBridge (Phase 1 Paper & Dry-Run Execution)
=====================================================================
Verifies order payload accuracy, risk guards, idempotency, kill-switch,
and multi-lot bracket partial exits.
"""

import pytest
import os
import shutil
from backend.execution.angel_execution_bridge import AngelExecutionBridge

TEST_AUDIT_DIR = "data/test_execution_logs"


@pytest.fixture
def bridge():
    # Setup test instance with a clean test audit dir
    b = AngelExecutionBridge(force_dry_run=True)
    b.audit_dir = TEST_AUDIT_DIR
    b.max_capital_per_trade = 25000.0
    b.max_active_orders = 3
    os.makedirs(TEST_AUDIT_DIR, exist_ok=True)
    yield b
    # Teardown
    if os.path.exists(TEST_AUDIT_DIR):
        shutil.rmtree(TEST_AUDIT_DIR, ignore_errors=True)


@pytest.mark.asyncio
async def test_nifty_order_payload_validation(bridge):
    """NIFTY option signal must route to exchange NFO with buffered limit price."""
    signal = {
        "signal_id": "SIG-TEST-NIFTY-CE-001",
        "instrument": "NIFTY",
        "direction": "CE",
        "strike": 22500.0,
        "option_type": "CE",
        "option_token": "44612",
        "option_symbol": "NIFTY13OCT2622500CE",
        "entry_price": 76.55,
        "quantity": 130,
        "lot_size": 65,
        "stop_loss": 61.55,
        "target": 106.55,
        "target_1r": 91.55,
        "target_2r": 106.55
    }

    is_valid, reason, prep_data = bridge.validate_signal(signal)
    assert is_valid is True
    assert reason == "VALIDATED_OK"

    payload = prep_data["broker_payload"]
    assert payload["exchange"] == "NFO"
    assert payload["tradingsymbol"] == "NIFTY13OCT2622500CE"
    assert payload["symboltoken"] == "44612"
    assert payload["transactiontype"] == "BUY"
    assert payload["producttype"] == "INTRADAY"
    assert payload["ordertype"] == "LIMIT"
    assert int(payload["quantity"]) == 130
    # Entry 76.55 * 1.005 = 76.93 -> rounded to 76.9
    assert float(payload["price"]) == round(76.55 * 1.005, 1)

    # Execute simulation
    res = await bridge.execute_signal(signal)
    assert res["status"] == "SIMULATED_FILLED"
    assert res["order_id"].startswith("DRY-")
    assert res["fill_price"] == 76.55


@pytest.mark.asyncio
async def test_sensex_order_payload_validation(bridge):
    """SENSEX option signal must route to exchange BFO."""
    signal = {
        "signal_id": "SIG-TEST-SENSEX-CE-002",
        "instrument": "SENSEX",
        "direction": "CE",
        "strike": 72400.0,
        "option_type": "CE",
        "option_token": "1101447",
        "option_symbol": "SENSEX26O1572400CE",
        "entry_price": 450.0,
        "quantity": 40,
        "lot_size": 20,
        "stop_loss": 390.0,
        "target": 570.0
    }

    is_valid, reason, prep_data = bridge.validate_signal(signal)
    assert is_valid is True
    assert prep_data["broker_payload"]["exchange"] == "BFO"

    res = await bridge.execute_signal(signal)
    assert res["status"] == "SIMULATED_FILLED"
    assert res["broker_payload"]["exchange"] == "BFO"


@pytest.mark.asyncio
async def test_idempotency_duplicate_protection(bridge):
    """Executing the exact same signal_id twice MUST reject the duplicate."""
    signal = {
        "signal_id": "SIG-IDEMPOTENT-003",
        "instrument": "NIFTY",
        "direction": "CE",
        "strike": 22500.0,
        "option_type": "CE",
        "option_token": "44612",
        "option_symbol": "NIFTY13OCT2622500CE",
        "entry_price": 60.0,
        "quantity": 65,
        "lot_size": 65,
        "stop_loss": 45.0,
        "target": 90.0
    }

    res1 = await bridge.execute_signal(signal)
    assert res1["status"] == "SIMULATED_FILLED"

    # Second execution attempt with identical signal_id
    res2 = await bridge.execute_signal(signal)
    assert res2["status"] == "REJECTED"
    assert "DUPLICATE_SIGNAL" in res2["reason"]


@pytest.mark.asyncio
async def test_quantity_lot_size_validation(bridge):
    """Orders with invalid quantity (not multiple of lot size) must be rejected."""
    bad_signal = {
        "signal_id": "SIG-BAD-QTY-004",
        "instrument": "NIFTY",
        "direction": "CE",
        "strike": 22500.0,
        "option_type": "CE",
        "option_token": "44612",
        "option_symbol": "NIFTY13OCT2622500CE",
        "entry_price": 50.0,
        "quantity": 100,  # 100 is NOT a multiple of Nifty lot size 65
        "lot_size": 65,
        "stop_loss": 40.0,
        "target": 80.0
    }

    is_valid, reason, _ = bridge.validate_signal(bad_signal)
    assert is_valid is False
    assert "NOT_MULTIPLE_OF_LOT_SIZE" in reason


@pytest.mark.asyncio
async def test_max_capital_cap_enforcement(bridge):
    """Orders exceeding max_capital_per_trade must be rejected."""
    bridge.max_capital_per_trade = 10000.0

    large_signal = {
        "signal_id": "SIG-OVER-CAP-005",
        "instrument": "NIFTY",
        "direction": "CE",
        "strike": 22500.0,
        "option_type": "CE",
        "option_token": "44612",
        "option_symbol": "NIFTY13OCT2622500CE",
        "entry_price": 100.0,
        "quantity": 130,  # Required capital = 130 * 100 = 13,000 > 10,000 limit
        "lot_size": 65,
        "stop_loss": 85.0,
        "target": 130.0
    }

    is_valid, reason, _ = bridge.validate_signal(large_signal)
    assert is_valid is False
    assert "CAPITAL_EXCEEDS_LIMIT" in reason


@pytest.mark.asyncio
async def test_kill_switch_protection(bridge):
    """When kill switch is activated, all order submissions must fail immediately."""
    bridge.activate_kill_switch(reason="Test Panic Switch")
    assert bridge.kill_switch_active is True

    signal = {
        "signal_id": "SIG-KILL-006",
        "instrument": "NIFTY",
        "direction": "CE",
        "strike": 22500.0,
        "option_type": "CE",
        "option_token": "44612",
        "option_symbol": "NIFTY13OCT2622500CE",
        "entry_price": 50.0,
        "quantity": 65,
        "lot_size": 65,
        "stop_loss": 40.0,
        "target": 80.0
    }

    res = await bridge.execute_signal(signal)
    assert res["status"] == "REJECTED"
    assert "KILL_SWITCH_ACTIVE" in res["reason"]

    # Deactivate and test re-enablement
    bridge.deactivate_kill_switch()
    assert bridge.kill_switch_active is False
    res2 = await bridge.execute_signal(signal)
    assert res2["status"] == "SIMULATED_FILLED"


@pytest.mark.asyncio
async def test_multi_lot_lifecycle_partial_exit(bridge):
    """Verifies entry of 2 lots, Target 1 exit of 1 lot (+1R), and breakeven trailing."""
    signal = {
        "signal_id": "SIG-LIFECYCLE-007",
        "instrument": "NIFTY",
        "direction": "CE",
        "strike": 22500.0,
        "option_type": "CE",
        "option_token": "44612",
        "option_symbol": "NIFTY13OCT2622500CE",
        "entry_price": 60.0,
        "quantity": 130,  # 2 lots
        "lot_size": 65,
        "stop_loss": 45.0,
        "target": 90.0,
        "target_1r": 75.0,
        "target_2r": 90.0
    }

    # 1. Entry
    entry_res = await bridge.execute_signal(signal)
    assert entry_res["status"] == "SIMULATED_FILLED"
    pos = bridge.active_positions[signal["signal_id"]]
    assert pos["remaining_quantity"] == 130

    # 2. Target 1 Partial Exit (Lot 1 booked at 75.0)
    p_exit = await bridge.handle_partial_exit(signal, exit_price=75.0)
    assert p_exit["action"] == "PARTIAL_EXIT_T1"
    assert p_exit["quantity_sold"] == 65
    assert p_exit["realized_pnl"] == (75.0 - 60.0) * 65  # +975.0
    assert p_exit["remaining_quantity"] == 65
    assert p_exit["breakeven_sl"] == 60.0

    # 3. Full Exit on remaining lot at Breakeven (60.0)
    f_exit = await bridge.handle_full_exit(signal, exit_price=60.0, reason="BREAKEVEN_SL")
    assert f_exit["action"] == "FULL_EXIT"
    assert f_exit["quantity_sold"] == 65
    assert f_exit["total_realized_pnl"] == 975.0  # Net profit preserved!
    assert signal["signal_id"] not in bridge.active_positions


@pytest.mark.asyncio
async def test_phase1_live_safety_lock(bridge):
    """In Phase 1, any attempt to bypass dry_run must be blocked with RuntimeError."""
    bridge.dry_run = False
    bridge.mode = "LIVE"

    signal = {
        "signal_id": "SIG-LOCKOUT-008",
        "instrument": "NIFTY",
        "direction": "CE",
        "strike": 22500.0,
        "option_type": "CE",
        "option_token": "44612",
        "option_symbol": "NIFTY13OCT2622500CE",
        "entry_price": 60.0,
        "quantity": 65,
        "lot_size": 65,
        "stop_loss": 45.0,
        "target": 90.0
    }

    with pytest.raises(RuntimeError, match="Live exchange order execution is strictly locked out in Phase 1"):
        await bridge.execute_signal(signal)
