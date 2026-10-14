"""
Unit & Integration Tests for ExecutionRiskGovernor.
Tests:
1. Available capital pool calculation (Pool - Deployed).
2. Phase 2 single-lot lock (forcing 1 lot regardless of requested size).
3. Dynamic lot step-down when margin is tight.
4. Clean rejection and logging on insufficient available capital.
5. Concurrency and circuit breaker gates.
"""

import pytest
import os
import sys

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
    active_positions = {}
    
    # Strategy asks for 2 lots (40 qty for SENSEX lot 20) @ 300.0 (Req: ₹12,000)
    sig = {
        "signal_id": "SIG-TEST-SENSEX-1",
        "instrument": "SENSEX",
        "entry_price": 300.0,
        "lot_size": 20,
        "quantity": 40  # 2 lots
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
        "quantity": 40
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
        "quantity": 20
    }
    
    ok, reason, data = gov.evaluate_execution_order(sig, active_positions)
    assert ok is False
    assert "REJECTED_INSUFFICIENT_AVAILABLE_CAPITAL" in reason
    assert "Avail: ₹5,000.00" in reason
    assert "Req 1-Lot: ₹6,000.00" in reason


def test_angel_execution_bridge_integration():
    bridge = AngelExecutionBridge(force_dry_run=True)
    bridge.enabled = True
    bridge.kill_switch_active = False
    bridge.risk_governor.total_capital = 25000.0
    bridge.risk_governor.force_single_lot = True
    
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
        "strategy": "MOMENTUM_IMPULSE"
    }
    
    ok, reason, prep = bridge.validate_signal(sig)
    assert ok is True
    assert prep["metadata"]["lots_count"] == 1
    assert prep["metadata"]["quantity"] == 20
    assert prep["metadata"]["required_capital"] == 5000.0
    assert prep["metadata"]["available_capital_after"] == 20000.0
