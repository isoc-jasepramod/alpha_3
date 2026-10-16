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
    # Setup test instance with a clean test audit dir and isolated test config
    os.makedirs(TEST_AUDIT_DIR, exist_ok=True)
    test_cfg_path = os.path.join(TEST_AUDIT_DIR, "test_settings.yaml")
    if os.path.exists("config/settings.yaml"):
        shutil.copy("config/settings.yaml", test_cfg_path)
    b = AngelExecutionBridge(config_path=test_cfg_path, force_dry_run=True, audit_dir=TEST_AUDIT_DIR)
    b.max_capital_per_trade = 25000.0
    b.max_active_orders = 3
    b.risk_governor.expiry_gate_enabled = False  # Avoid wall-clock Friday cutoff during testing
    b.risk_governor.force_single_lot = False     # Allow multi-lot verification in test suite
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
    # Buffered price aligned to 0.05 tick size
    expected_price = bridge.align_tick_size(76.55 * (1.0 + bridge.slippage_buffer_pct / 100.0), 0.05)
    assert float(payload["price"]) == expected_price

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
async def test_execution_mode_toggle(bridge):
    """Verifies seamless switching between Phase 1 Paper and Phase 2 Live modes."""
    # Default is PAPER
    assert bridge.mode == "PAPER"
    assert bridge.dry_run is True

    # Toggle to LIVE
    res_live = bridge.set_execution_mode("LIVE")
    assert res_live["status"] == "SUCCESS"
    assert bridge.mode == "LIVE"
    assert bridge.dry_run is False

    # Toggle back to PAPER (e.g. Monday morning change of mind)
    res_paper = bridge.set_execution_mode("PAPER")
    assert res_paper["status"] == "SUCCESS"
    assert bridge.mode == "PAPER"
    assert bridge.dry_run is True


@pytest.mark.asyncio
async def test_live_execution_unauthenticated_guard(bridge):
    """When in LIVE mode without broker session, orders must be safely rejected."""
    bridge.set_execution_mode("LIVE")
    bridge.auth = None

    signal = {
        "signal_id": "SIG-LIVE-UNAUTH-008",
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

    res = await bridge.execute_signal(signal)
    assert res["status"] == "REJECTED"
    assert "REJECTED_BROKER_UNAUTHENTICATED" in res["reason"]


@pytest.mark.asyncio
async def test_phase2_live_order_routing_and_lifecycle(bridge):
    """Phase 2: Validates live order routing to Angel One SmartAPI and live exit placement."""
    from unittest.mock import MagicMock

    # Setup mock SmartConnect on auth
    mock_sc = MagicMock()
    mock_sc.placeOrder.return_value = "24101600098765"
    mock_auth = MagicMock()
    mock_auth.smart_connect = mock_sc
    bridge.auth = mock_auth

    # Switch to LIVE mode
    bridge.set_execution_mode("LIVE")

    signal = {
        "signal_id": "SIG-PHASE2-LIVE-009",
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

    # 1. Live Entry Order Execution
    entry_res = await bridge.execute_signal(signal)
    assert entry_res["status"] == "LIVE_FILLED"
    assert entry_res["order_id"] == "24101600098765"
    assert mock_sc.placeOrder.call_count == 1

    # Verify order payload sent to Angel One
    placed_payload = mock_sc.placeOrder.call_args[0][0]
    assert placed_payload["exchange"] == "NFO"
    assert placed_payload["tradingsymbol"] == "NIFTY13OCT2622500CE"
    assert placed_payload["transactiontype"] == "BUY"
    assert int(placed_payload["quantity"]) == 130

    pos = bridge.active_positions[signal["signal_id"]]
    assert pos["mode"] == "LIVE_EXCHANGE"
    assert pos["remaining_quantity"] == 130

    # 2. Live Target 1 Partial Exit (Lot 1)
    mock_sc.placeOrder.return_value = "24101600098766"
    p_exit = await bridge.handle_partial_exit(signal, exit_price=75.0)
    assert p_exit["action"] == "PARTIAL_EXIT_T1"
    assert p_exit["quantity_sold"] == 65
    assert p_exit["realized_pnl"] == (75.0 - 60.0) * 65

    # Verify SELL payload sent to Angel One for Lot 1
    sell_payload_1 = mock_sc.placeOrder.call_args[0][0]
    assert sell_payload_1["transactiontype"] == "SELL"
    assert int(sell_payload_1["quantity"]) == 65

    # 3. Live Full Exit (Lot 2 at Breakeven)
    mock_sc.placeOrder.return_value = "24101600098767"
    f_exit = await bridge.handle_full_exit(signal, exit_price=60.0, reason="BREAKEVEN_SL")
    assert f_exit["action"] == "FULL_EXIT"
    assert f_exit["quantity_sold"] == 65
    assert f_exit["total_realized_pnl"] == (75.0 - 60.0) * 65
    assert signal["signal_id"] not in bridge.active_positions

    # Verify SELL payload sent to Angel One for Lot 2
    sell_payload_2 = mock_sc.placeOrder.call_args[0][0]
    assert sell_payload_2["transactiontype"] == "SELL"
    assert int(sell_payload_2["quantity"]) == 65


def test_api_mode_toggle_endpoint(bridge):
    """Verifies FastAPI route /api/execution/mode switches execution mode dynamically."""
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from backend.api.routes import router, app_state

    app = FastAPI()
    app.include_router(router)
    app_state.execution_bridge = bridge
    client = TestClient(app)

    # Check default status
    res = client.get("/api/execution/status")
    assert res.status_code == 200
    assert res.json()["mode"] == "PAPER"
    assert res.json()["dry_run"] is True

    # Toggle to LIVE via query parameter ?mode=LIVE
    res_toggle = client.post("/api/execution/mode?mode=LIVE")
    assert res_toggle.status_code == 200
    assert res_toggle.json()["status"] == "SUCCESS"
    assert res_toggle.json()["mode"] == "LIVE"
    assert res_toggle.json()["dry_run"] is False

    # Check status endpoint reflects LIVE
    res_status2 = client.get("/api/execution/status")
    assert res_status2.json()["mode"] == "LIVE"
    assert res_status2.json()["dry_run"] is False

    # Toggle back to PAPER via JSON body {"mode": "PAPER"}
    res_toggle_paper = client.post("/api/execution/mode", json={"mode": "PAPER"})
    assert res_toggle_paper.status_code == 200
    assert res_toggle_paper.json()["mode"] == "PAPER"
    assert res_toggle_paper.json()["dry_run"] is True

    # Verify status is back to PAPER
    res_status3 = client.get("/api/execution/status")
    assert res_status3.json()["mode"] == "PAPER"
    assert res_status3.json()["dry_run"] is True


def test_tick_size_alignment():
    """Validates that align_tick_size rounds strictly to 0.05 multiples for Indian options."""
    assert AngelExecutionBridge.align_tick_size(76.53, 0.05) == 76.55
    assert AngelExecutionBridge.align_tick_size(76.52, 0.05) == 76.50
    assert AngelExecutionBridge.align_tick_size(76.93275, 0.05) == 76.95
    assert AngelExecutionBridge.align_tick_size(100.01, 0.05) == 100.00
    assert AngelExecutionBridge.align_tick_size(100.03, 0.05) == 100.05
    assert AngelExecutionBridge.align_tick_size(0.01, 0.05) == 0.05
    assert AngelExecutionBridge.align_tick_size(-5.0, 0.05) == 0.05


@pytest.mark.asyncio
async def test_order_rejected_by_broker_rms(bridge):
    """
    When broker RMS rejects an order (e.g. margin shortfall or strike band lock),
    the bridge must record the rejection in audit logs, publish REJECTED event,
    and NOT create any active position or allocate margin.
    """
    from unittest.mock import MagicMock

    # Setup mock broker rejecting the order with RMS error
    mock_sc = MagicMock()
    mock_sc.placeOrder.return_value = {
        "status": False,
        "message": "RMS: Margin Insufficient. Required: 25000, Available: 12000",
        "errorcode": "AB1004"
    }
    mock_auth = MagicMock()
    mock_auth.smart_connect = mock_sc
    bridge.auth = mock_auth
    bridge.set_execution_mode("LIVE")

    signal = {
        "signal_id": "SIG-RMS-REJECT-001",
        "instrument": "NIFTY",
        "direction": "CE",
        "strike": 22500.0,
        "option_type": "CE",
        "option_token": "44612",
        "option_symbol": "NIFTY13OCT2622500CE",
        "entry_price": 80.0,
        "quantity": 65,
        "lot_size": 65,
        "stop_loss": 60.0,
        "target": 120.0
    }

    res = await bridge.execute_signal(signal)
    assert res["status"] == "REJECTED"
    assert "BROKER_ERROR: RMS: Margin Insufficient" in res["reason"]

    # Verify no position is created in memory
    assert "SIG-RMS-REJECT-001" not in bridge.active_positions
    assert "SIG-RMS-REJECT-001" not in bridge.signal_to_order

    # Verify audit log recorded the rejection
    today_records = bridge.get_today_orders()
    rejections = [r for r in today_records if r.get("signal_id") == "SIG-RMS-REJECT-001"]
    assert len(rejections) == 1
    assert rejections[0]["status"] == "REJECTED_BY_BROKER"
    assert "RMS: Margin Insufficient" in rejections[0]["reason"]


@pytest.mark.asyncio
async def test_exit_order_broker_failure_preserves_position(bridge):
    """
    If an exit order fails at the broker (e.g. API disconnect or exchange rejection),
    the position MUST NOT be removed from active_positions. It should be flagged
    with exit_failed=True so the trader can safely retry square-off.
    """
    from unittest.mock import MagicMock

    mock_sc = MagicMock()
    mock_auth = MagicMock()
    mock_auth.smart_connect = mock_sc
    bridge.auth = mock_auth
    bridge.set_execution_mode("LIVE")

    # 1. Successful entry
    mock_sc.placeOrder.return_value = "24101600099901"
    signal = {
        "signal_id": "SIG-EXIT-FAIL-001",
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
        "target": 70.0
    }

    entry_res = await bridge.execute_signal(signal)
    assert entry_res["status"] == "LIVE_FILLED"
    assert "SIG-EXIT-FAIL-001" in bridge.active_positions

    # 2. Broker returns rejection on exit order
    mock_sc.placeOrder.return_value = {
        "status": False,
        "message": "Exchange Gateway Timeout. Order not processed.",
        "errorcode": "AG500"
    }

    exit_res = await bridge.handle_full_exit(signal, exit_price=40.0, reason="STOP_LOSS")
    assert exit_res["status"] == "EXIT_FAILED"
    assert "Exchange Gateway Timeout" in exit_res["reason"]

    # CRITICAL: Position must STILL be held in active_positions to prevent untracked money exposure
    assert "SIG-EXIT-FAIL-001" in bridge.active_positions
    held_pos = bridge.active_positions["SIG-EXIT-FAIL-001"]
    assert held_pos["exit_failed"] is True
    assert "Exchange Gateway Timeout" in held_pos["last_exit_error"]


def test_safe_production_defaults():
    """
    P0-1 Blocker Verification:
    Asserts that the committed settings.yaml ships with safe defaults:
    mode: PAPER, dry_run: True, force_single_lot: True.
    A cold boot must never place real money trades.
    """
    import yaml
    with open("config/settings.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    exec_cfg = cfg.get("execution", {})

    assert exec_cfg.get("mode") == "PAPER"
    assert exec_cfg.get("dry_run") is True
    assert exec_cfg.get("force_single_lot") is True


@pytest.mark.asyncio
async def test_live_entry_fill_confirmation_real_price_anchors_sl_target(bridge):
    """
    P0-2 & P0-4 Verification:
    Asserts that after placeOrder returns an order_id, the bridge confirms fill via
    smart_connect.orderBook(), records the actual executed averageprice, and re-anchors
    stop-loss and profit targets to the REAL fill price.
    """
    from unittest.mock import MagicMock

    mock_sc = MagicMock()
    mock_sc.placeOrder.return_value = "24101600099881"
    # Broker fills at ₹62.50 instead of expected entry ₹60.00
    mock_sc.orderBook.return_value = {
        "status": True,
        "data": [
            {
                "orderid": "24101600099881",
                "status": "complete",
                "averageprice": 62.50,
                "filledshares": 65
            }
        ]
    }
    mock_auth = MagicMock()
    mock_auth.smart_connect = mock_sc
    bridge.auth = mock_auth
    bridge.set_execution_mode("LIVE")

    signal = {
        "signal_id": "SIG-REAL-FILL-001",
        "instrument": "NIFTY",
        "direction": "CE",
        "strike": 22500.0,
        "option_type": "CE",
        "option_token": "44612",
        "option_symbol": "NIFTY13OCT2622500CE",
        "entry_price": 60.0,
        "quantity": 65,
        "lot_size": 65,
        "stop_loss": 45.0,  # 15.00 delta
        "target": 90.0
    }

    res = await bridge.execute_signal(signal)
    assert res["status"] == "LIVE_FILLED"
    assert res["fill_price"] == 62.50

    pos = bridge.active_positions["SIG-REAL-FILL-001"]
    assert pos["fill_price"] == 62.50
    # Re-anchored with 15.00 risk delta from real fill price (62.50)
    assert pos["stop_loss"] == 47.50   # 62.50 - 15.00
    assert pos["target_1r"] == 77.50   # 62.50 + 15.00
    assert pos["target_2r"] == 92.50   # 62.50 + 30.00


@pytest.mark.asyncio
async def test_live_entry_unfilled_timeout_aborts_and_cancels(bridge):
    """
    P0-3 Verification:
    If a limit order sits unfilled until limit_timeout_sec expires, the bridge
    must send cancelOrder to the broker and abort the trade with ZERO active positions.
    """
    from unittest.mock import MagicMock

    mock_sc = MagicMock()
    mock_sc.placeOrder.return_value = "24101600099882"
    # Order stays OPEN on the exchange
    mock_sc.orderBook.return_value = {
        "status": True,
        "data": [
            {
                "orderid": "24101600099882",
                "status": "open",
                "filledshares": 0
            }
        ]
    }
    mock_sc.cancelOrder.return_value = {"status": True, "message": "SUCCESS"}
    mock_auth = MagicMock()
    mock_auth.smart_connect = mock_sc
    bridge.auth = mock_auth
    bridge.set_execution_mode("LIVE")
    bridge.limit_timeout_sec = 1  # 1s timeout for fast test

    signal = {
        "signal_id": "SIG-TIMEOUT-001",
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
        "target": 70.0
    }

    res = await bridge.execute_signal(signal)
    assert res["status"] == "ABORTED_UNFILLED"
    assert "LIMIT_TIMEOUT" in res["reason"]

    # Verify cancelOrder was dispatched to broker
    assert mock_sc.cancelOrder.call_count == 1
    # Verify no active position was created
    assert "SIG-TIMEOUT-001" not in bridge.active_positions


@pytest.mark.asyncio
async def test_live_exit_fill_confirmation_computes_real_pnl(bridge):
    """
    P1-2 Verification:
    When an exit order fills on the broker, realized P&L must be computed
    from the actual broker fill price, not the theoretical target level.
    """
    from unittest.mock import MagicMock

    mock_sc = MagicMock()
    mock_auth = MagicMock()
    mock_auth.smart_connect = mock_sc
    bridge.auth = mock_auth
    bridge.set_execution_mode("LIVE")

    # 1. Entry fill confirmed at 50.00
    mock_sc.placeOrder.return_value = "24101600099883"
    mock_sc.orderBook.return_value = {
        "status": True,
        "data": [{"orderid": "24101600099883", "status": "complete", "averageprice": 50.0, "filledshares": 65}]
    }
    signal = {
        "signal_id": "SIG-EXIT-REAL-PNL",
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
        "target": 65.0
    }
    await bridge.execute_signal(signal)

    # 2. Exit fill confirmed at 58.40 (instead of target 65.00)
    mock_sc.placeOrder.return_value = "24101600099884"
    mock_sc.orderBook.return_value = {
        "status": True,
        "data": [{"orderid": "24101600099884", "status": "complete", "averageprice": 58.40, "filledshares": 65}]
    }

    exit_res = await bridge.handle_full_exit(signal, exit_price=65.0, reason="TARGET_HIT")
    assert exit_res["action"] == "FULL_EXIT"
    assert exit_res["exit_price"] == 58.40
    # Realized PnL: (58.40 - 50.00) * 65 = 8.40 * 65 = 546.00
    assert exit_res["total_realized_pnl"] == round((58.40 - 50.00) * 65, 2)
    assert signal["signal_id"] not in bridge.active_positions


@pytest.mark.asyncio
async def test_orders_persisted_to_database(bridge):
    """Verifies that placed orders are persisted to the PostgreSQL 'orders' table."""
    from backend.database.db import init_db
    await init_db()

    signal = {
        "signal_id": "SIG-DB-TEST-001",
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
        "target": 65.0
    }
    try:
        res = await bridge.execute_signal(signal)
        assert res["status"] in ("FILLED", "SIMULATED_FILLED")
        order_id = res["order_id"]

        await bridge._persist_order_to_db(bridge.placed_orders[order_id])

        db_orders = await bridge.get_orders_from_db()
        matching = [o for o in db_orders if o["order_id"] == order_id]
        assert len(matching) == 1
        assert matching[0]["signal_id"] == "SIG-DB-TEST-001"
        assert matching[0]["instrument"] == "NIFTY"
        assert matching[0]["quantity"] == 65
        assert matching[0]["fill_price"] == 50.0
        assert matching[0]["status"] == "FILLED"
    finally:
        from backend.database.db import engine
        await engine.dispose()


@pytest.mark.asyncio
async def test_exit_updates_parent_order_in_database(bridge):
    """Verifies that an exit order updates the parent order's status, exit_price, and PnL in the DB."""
    from backend.database.db import init_db, engine
    await init_db()

    try:
        signal = {
            "signal_id": "SIG-DB-EXIT-001",
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
            "target": 65.0
        }
        res = await bridge.execute_signal(signal)
        order_id = res["order_id"]
        await bridge._persist_order_to_db(bridge.placed_orders[order_id])

        # Execute full exit at 65.0
        exit_res = await bridge.handle_full_exit(signal, exit_price=65.0, reason="TARGET_HIT")
        await bridge._persist_order_to_db(exit_res)

        db_orders = await bridge.get_orders_from_db()
        matching_parent = [o for o in db_orders if o["order_id"] == order_id]
        assert len(matching_parent) == 1
        assert matching_parent[0]["status"] == "CLOSED"
        assert matching_parent[0]["exit_price"] == 65.0
        assert matching_parent[0]["total_realized_pnl"] == round((65.0 - 50.0) * 65, 2)
        assert matching_parent[0]["closed_at"] is not None
    finally:
        await engine.dispose()






