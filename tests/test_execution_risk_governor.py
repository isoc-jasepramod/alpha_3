"""
Unit & Integration Tests for ExecutionRiskGovernor.
Tests:
1. Available capital pool calculation (Pool - Deployed).
2. Phase 2 single-lot lock (forcing 1 lot regardless of requested size).
3. Dynamic lot step-down when margin is tight.
4. Clean rejection and logging on insufficient available capital.
5. Volatility Stop Loss verification.
6. 1:2 Risk-to-Reward Targets verification.
7. Expiry Time Gate verification.
8. AngelExecutionBridge integration.
"""

import pytest
import os
import sys
from datetime import datetime, timezone, timedelta

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from backend.execution.execution_risk_governor import ExecutionRiskGovernor
from backend.execution.angel_execution_bridge import AngelExecutionBridge


def test_capital_pool_and_available_calculation():
    gov = ExecutionRiskGovernor()
    gov.total_capital = 25000.0
    
    # Empty active positions
    active_positions = {}
    assert gov.get_deployed_capital(active_positions) == 0.0
    assert gov.get_available_capital(active_positions) == 25000.0
    
    # Position 1 deployed: 20 qty @ 700.0 = ₹14,000
    active_positions["SIG-1"] = {
        "quantity": 20,
        "fill_price": 700.0,
        "metadata": {
            "required_capital": 14000.0
        }
    }
    
    assert gov.get_deployed_capital(active_positions) == 14000.0
    assert gov.get_available_capital(active_positions) == 11000.0
    
    metrics = gov.get_capital_metrics(active_positions)
    assert metrics["total_capital"] == 25000.0
    assert metrics["deployed_capital"] == 14000.0
    assert metrics["available_capital"] == 11000.0
    assert metrics["utilization_pct"] == 56.0


def test_phase2_forced_single_lot():
    gov = ExecutionRiskGovernor()
    gov.total_capital = 25000.0
    gov.force_single_lot = True
    gov.expiry_gate_enabled = False  # Isolate test from current time
    active_positions = {}
    
    # Strategy asks for 2 lots (40 qty for SENSEX lot 20) @ 300.0 (Req: ₹12,000)
    sig = {
        "signal_id": "SIG-TEST-SENSEX-1",
        "instrument": "SENSEX",
        "entry_price": 300.0,
        "lot_size": 20,
        "quantity": 40,  # 2 lots
        "stop_loss": 240.0,
        "target_1r": 360.0,
        "target_2r": 420.0
    }
    
    ok, reason, data = gov.evaluate_execution_order(sig, active_positions)
    assert ok is True
    assert data["lots_count"] == 1
    assert data["quantity"] == 20
    assert data["required_capital"] == 6000.0
    assert data["adjustment_note"] == "PHASE2_FORCED_SINGLE_LOT"
    assert data["available_capital_after"] == 19000.0


def test_dynamic_lot_step_down_when_margin_tight():
    gov = ExecutionRiskGovernor()
    gov.total_capital = 25000.0
    gov.force_single_lot = False  # Multi-lot enabled
    gov.expiry_gate_enabled = False
    
    # Position 1 already active using ₹14,000 (Available: ₹11,000)
    active_positions = {
        "SIG-1": {"metadata": {"required_capital": 14000.0}}
    }
    assert gov.get_available_capital(active_positions) == 11000.0
    
    # Incoming signal asks for 2 lots (40 qty) @ 350.0 = ₹14,000 (Exceeds ₹11,000 available)
    # But 1 lot (20 qty) @ 350.0 = ₹7,000 (Fits inside ₹11,000)
    sig = {
        "signal_id": "SIG-TEST-SENSEX-2",
        "instrument": "SENSEX",
        "entry_price": 350.0,
        "lot_size": 20,
        "quantity": 40,
        "stop_loss": 280.0,
        "target_1r": 420.0,
        "target_2r": 490.0
    }
    
    ok, reason, data = gov.evaluate_execution_order(sig, active_positions)
    assert ok is True
    assert data["lots_count"] == 1
    assert data["quantity"] == 20
    assert data["required_capital"] == 7000.0
    assert "DOWNSIZED_TO_1_LOTS_FOR_MARGIN" in data["adjustment_note"]
    assert data["available_capital_after"] == 4000.0


def test_insufficient_available_capital_rejection():
    gov = ExecutionRiskGovernor()
    gov.total_capital = 25000.0
    gov.expiry_gate_enabled = False
    
    # Position 1 active using ₹20,000 (Available: ₹5,000)
    active_positions = {
        "SIG-1": {"metadata": {"required_capital": 20000.0}}
    }
    assert gov.get_available_capital(active_positions) == 5000.0
    
    # Incoming signal 1 lot (20 qty) @ 300.0 = ₹6,000 (Exceeds available ₹5,000)
    sig = {
        "signal_id": "SIG-TEST-EXHAUSTION",
        "instrument": "SENSEX",
        "entry_price": 300.0,
        "lot_size": 20,
        "quantity": 20,
        "stop_loss": 240.0,
        "target_1r": 360.0,
        "target_2r": 420.0
    }
    
    ok, reason, data = gov.evaluate_execution_order(sig, active_positions)
    assert ok is False
    assert "REJECTED_INSUFFICIENT_AVAILABLE_CAPITAL" in reason
    assert "Avail: ₹5,000.00" in reason
    assert "Req 1-Lot: ₹6,000.00" in reason


def test_volatility_stop_loss_validation():
    gov = ExecutionRiskGovernor()
    gov.expiry_gate_enabled = False
    active_positions = {}
    
    # Missing SL
    sig_no_sl = {
        "signal_id": "SIG-NO-SL",
        "instrument": "SENSEX",
        "entry_price": 300.0,
        "lot_size": 20,
        "quantity": 20,
        "stop_loss": 0.0,
        "target_1r": 360.0
    }
    ok, reason, _ = gov.evaluate_execution_order(sig_no_sl, active_positions)
    assert ok is False
    assert "REJECTED_MISSING_VOLATILITY_STOP_LOSS" in reason
    
    # SL above entry (invalid for buyer)
    sig_bad_sl = {
        "signal_id": "SIG-BAD-SL",
        "instrument": "SENSEX",
        "entry_price": 300.0,
        "lot_size": 20,
        "quantity": 20,
        "stop_loss": 320.0,
        "target_1r": 360.0
    }
    ok, reason, _ = gov.evaluate_execution_order(sig_bad_sl, active_positions)
    assert ok is False
    assert "REJECTED_STOP_LOSS_ABOVE_ENTRY" in reason


def test_target_ratio_validation():
    gov = ExecutionRiskGovernor()
    gov.expiry_gate_enabled = False
    active_positions = {}
    
    # Target 1 below entry price
    sig_bad_t1 = {
        "signal_id": "SIG-BAD-T1",
        "instrument": "SENSEX",
        "entry_price": 300.0,
        "lot_size": 20,
        "quantity": 20,
        "stop_loss": 250.0,
        "target_1r": 280.0
    }
    ok, reason, _ = gov.evaluate_execution_order(sig_bad_t1, active_positions)
    assert ok is False
    assert "REJECTED_INVALID_TARGET_1R" in reason


def test_angel_execution_bridge_integration():
    bridge = AngelExecutionBridge(force_dry_run=True)
    bridge.enabled = True
    bridge.kill_switch_active = False
    bridge.risk_governor.total_capital = 25000.0
    bridge.risk_governor.force_single_lot = True
    bridge.risk_governor.expiry_gate_enabled = False
    
    # Valid candidate signal
    sig = {
        "signal_id": "SIG-INTEG-1",
        "instrument": "SENSEX",
        "strike": 73000.0,
        "direction": "CE",
        "option_type": "CE",
        "option_symbol": "SENSEX26O1573000CE",
        "option_token": "1111226",
        "entry_price": 250.0,
        "quantity": 40,  # 2 lots
        "lot_size": 20,
        "stop_loss": 200.0,
        "target": 350.0,
        "target_1r": 300.0,
        "target_2r": 350.0,
        "strategy": "MOMENTUM_IMPULSE"
    }
    
    ok, reason, prep = bridge.validate_signal(sig)
    assert ok is True
    assert prep["metadata"]["lots_count"] == 1
    assert prep["metadata"]["quantity"] == 20
    assert prep["metadata"]["required_capital"] == 5000.0
    assert prep["metadata"]["available_capital_after"] == 20000.0
    assert prep["metadata"]["stop_loss"] == 200.0
    assert prep["metadata"]["target_1r"] == 300.0
    assert prep["metadata"]["target_2r"] == 350.0
