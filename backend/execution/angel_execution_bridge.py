"""
PROJECT ALPHA 3.0 — ANGEL ONE EXECUTION BRIDGE (PHASE 1: PAPER & DRY-RUN)
========================================================================
Production-grade execution management bridge connecting Project Alpha 3.0's 
Signal Generation Engine with AngelOne SmartAPI.

PHASE 1 SPECIFICATION:
----------------------
- Strict DRY_RUN & PAPER TRADING mode (Zero real-money exchange risk).
- Precision order payload validation and symbol/token mapping.
- Exchange routing: NFO for NIFTY options, BFO for SENSEX options.
- Slippage Protection: Buffered limit order calculation (LTP + buffer%).
- Multi-Tier Risk Guards:
    * Capital per trade limits.
    * Daily maximum loss threshold enforcement.
    * Idempotency Guard (guarantees zero duplicate order submissions per signal ID).
    * Active positions ceiling (max concurrent positions limit).
    * Emergency Kill Switch with instant order rejection.
- Bracket & Partial Exit Lifecycle management (Lot 1 at Target 1, Lot 2 at Breakeven).
- Comprehensive JSON audit trail logging.
"""

import os
import sys
import json
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, List
from loguru import logger
import yaml
from backend.execution.execution_risk_governor import ExecutionRiskGovernor

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)

DEFAULT_CONFIG_PATH = os.path.join(project_root, "config", "settings.yaml")


class AngelExecutionBridge:
    """
    Manages order preparation, risk gating, and dry-run execution 
    for AngelOne SmartAPI.
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        auth: Optional[Any] = None,
        redis_bus: Optional[Any] = None,
        force_dry_run: bool = True
    ):
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self.auth = auth
        self.redis_bus = redis_bus
        self.force_dry_run = force_dry_run

        # Core State
        self.placed_orders: Dict[str, Dict[str, Any]] = {}       # order_id -> order_record
        self.signal_to_order: Dict[str, str] = {}                 # signal_id -> order_id (idempotency)
        self.active_positions: Dict[str, Dict[str, Any]] = {}    # signal_id -> active_position
        self.kill_switch_active: bool = False

        # Dedicated Execution Risk Governor (Decoupled from Live Advisory)
        self.risk_governor = ExecutionRiskGovernor(self.config_path)

        self._load_config()

        # Audit directory
        self.audit_dir = os.path.join(project_root, self.audit_log_dir)
        os.makedirs(self.audit_dir, exist_ok=True)

        logger.info(
            f"🛡️ [EXECUTION BRIDGE] Initialized. Mode: {self.mode} | "
            f"DryRun: {self.dry_run} | Capital Pool: ₹{self.risk_governor.total_capital:,.0f} | "
            f"KillSwitch: {'ACTIVE' if self.kill_switch_active else 'OFF'}"
        )

    def _load_config(self):
        """Loads execution configuration from settings.yaml."""
        cfg = {}
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    full_cfg = yaml.safe_load(f) or {}
                    cfg = full_cfg.get("execution", {})
            except Exception as e:
                logger.warning(f"Failed to load execution config from {self.config_path}: {e}")

        self.enabled = bool(cfg.get("enabled", True))
        self.mode = str(cfg.get("mode", "PAPER")).upper()
        # In Phase 1, dry_run is hard-locked to True if force_dry_run is set
        self.dry_run = True if self.force_dry_run else bool(cfg.get("dry_run", True))
        self.default_product_type = str(cfg.get("default_product_type", "INTRADAY")).upper()
        self.default_order_type = str(cfg.get("default_order_type", "LIMIT")).upper()
        self.slippage_buffer_pct = float(cfg.get("slippage_buffer_pct", 0.5))
        self.max_capital_per_trade = float(cfg.get("max_capital_per_trade", 25000.0))
        self.max_daily_loss = float(cfg.get("max_daily_loss", 4000.0))
        self.max_active_orders = int(cfg.get("max_active_orders", 2))
        self.kill_switch_active = bool(cfg.get("kill_switch_active", False))
        self.audit_log_dir = str(cfg.get("audit_log_dir", "data/execution_logs"))
        self.allowed_strategies = cfg.get("allowed_strategies", [
            "OI_SQUEEZE", "ORB_BREAKOUT", "VWAP_EMA", "SIMPLIFIED_VWAP_EMA", "GAMMA_SCALP", "MOMENTUM_IMPULSE"
        ])
        self.nifty_lots = int(cfg.get("nifty_lots", 1))
        self.sensex_lots = int(cfg.get("sensex_lots", 1))
        if hasattr(self, "risk_governor") and self.risk_governor:
            self.risk_governor.load_config()

    def activate_kill_switch(self, reason: str = "Manual / Automated Trigger"):
        """Instantly activates the emergency kill switch, blocking all new orders."""
        self.kill_switch_active = True
        logger.critical(f"🛑 [KILL SWITCH ACTIVATED] Reason: {reason}. All automated execution halted!")

    def deactivate_kill_switch(self):
        """Resets the kill switch."""
        self.kill_switch_active = False
        logger.warning("🟢 [KILL SWITCH DEACTIVATED] Automated execution re-enabled.")

    async def square_off_all_positions(self, reason: str = "KILL_SWITCH") -> List[Dict[str, Any]]:
        """Squares off all active open positions immediately."""
        closed = []
        sig_ids = list(self.active_positions.keys())
        for sig_id in sig_ids:
            pos = self.active_positions.get(sig_id)
            if pos:
                exit_price = pos.get("current_ltp", pos.get("fill_price", 0.0))
                evt = await self.handle_full_exit({"signal_id": sig_id}, exit_price=exit_price, reason=reason)
                closed.append(evt)
        return closed

    def validate_signal(self, signal: Dict[str, Any]) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Validates signal parameters against risk limits, broker constraints, 
        and exchange formatting standards.
        """
        # 1. Kill Switch Check
        if self.kill_switch_active:
            return False, "REJECTED_KILL_SWITCH_ACTIVE", None

        # 2. Execution Enablement Check
        if not self.enabled:
            return False, "REJECTED_EXECUTION_DISABLED", None

        # 3. Strategy Filter Check
        strategy_name = signal.get("strategy")
        if strategy_name and self.allowed_strategies:
            alias_map = {
                "OI_SQUEEZE_SENTINEL": "OI_SQUEEZE",
                "BREAKOUT_ORB": "ORB_BREAKOUT",
                "VWAP_EMA_ALIGNMENT": "VWAP_EMA",
            }
            allowed_set = set(self.allowed_strategies)
            for k, v in alias_map.items():
                if k in allowed_set:
                    allowed_set.add(v)
                if v in allowed_set:
                    allowed_set.add(k)
            if strategy_name not in allowed_set:
                return False, f"REJECTED_STRATEGY_NOT_ALLOWED ({strategy_name})", None

        # 4. Required Fields
        req_keys = [
            "signal_id", "instrument", "strike", "direction", 
            "option_type", "option_token", "option_symbol", 
            "entry_price", "quantity", "lot_size"
        ]
        for k in req_keys:
            if k not in signal or signal[k] is None:
                return False, f"REJECTED_MISSING_KEY_{k.upper()}", None

        sig_id = str(signal["signal_id"])

        # 5. Idempotency Check (Zero duplicate submissions)
        if sig_id in self.signal_to_order:
            existing_ord = self.signal_to_order[sig_id]
            return False, f"REJECTED_DUPLICATE_SIGNAL (Already ordered as {existing_ord})", None

        # 6. Instrument & Exchange Mapping
        instrument = str(signal["instrument"]).upper()
        if instrument == "NIFTY":
            exchange = "NFO"
        elif instrument == "SENSEX":
            exchange = "BFO"
        else:
            return False, f"REJECTED_UNSUPPORTED_INSTRUMENT_{instrument}", None

        # 7. Execution Risk Governor: Concurrency, Available Margin Pool, & Lot Sizing
        risk_ok, risk_reason, risk_data = self.risk_governor.evaluate_execution_order(
            signal, self.active_positions
        )
        if not risk_ok:
            return False, risk_reason, None

        qty = int(risk_data["quantity"])
        lots_count = int(risk_data["lots_count"])
        required_capital = float(risk_data["required_capital"])
        lot_size = int(signal["lot_size"])
        entry_price = float(signal["entry_price"])

        # 8. Slippage-Buffered Limit Price
        buffer_multiplier = 1.0 + (self.slippage_buffer_pct / 100.0)
        buffered_limit_price = round(entry_price * buffer_multiplier, 1)

        prepared_payload = {
            "variety": "NORMAL",
            "tradingsymbol": str(signal["option_symbol"]),
            "symboltoken": str(signal["option_token"]),
            "transactiontype": "BUY",
            "exchange": exchange,
            "ordertype": self.default_order_type,
            "producttype": self.default_product_type,
            "duration": "DAY",
            "price": str(buffered_limit_price),
            "quantity": str(qty)
        }

        metadata = {
            "signal_id": sig_id,
            "instrument": instrument,
            "strike": float(signal["strike"]),
            "direction": str(signal["direction"]),
            "strategy": str(signal.get("strategy", "")),
            "option_symbol": str(signal["option_symbol"]),
            "option_token": str(signal["option_token"]),
            "lot_size": lot_size,
            "quantity": qty,
            "lots_count": lots_count,
            "expected_entry": entry_price,
            "buffered_limit_price": buffered_limit_price,
            "required_capital": required_capital,
            "available_capital_after": risk_data.get("available_capital_after"),
            "adjustment_note": risk_data.get("adjustment_note"),
            "exit_mode": risk_data.get("exit_mode", "TARGET_1_FULL_EXIT" if lots_count == 1 else "1_TO_2_SCALE_AND_TRAIL"),
            "exit_guidance": risk_data.get("exit_guidance", ""),
            "stop_loss": float(signal.get("stop_loss", 0.0)),
            "target": float(risk_data.get("target") or signal.get("target_1r" if lots_count == 1 else "target_2r", 0.0)),
            "target_1r": float(signal.get("target_1r") or signal.get("target", 0.0)),
            "target_2r": float(signal.get("target_2r") or signal.get("target", 0.0))
        }

        return True, "VALIDATED_OK", {
            "broker_payload": prepared_payload,
            "metadata": metadata
        }

    async def execute_signal(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        """
        Processes a signal, validates risk gates, and simulates execution (Dry-Run / Paper).
        """
        is_valid, reason, prep_data = self.validate_signal(signal)
        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.isoformat()

        if not is_valid:
            logger.warning(f"❌ [ORDER REJECTED] {signal.get('signal_id')}: {reason}")
            return {
                "status": "REJECTED",
                "signal_id": signal.get("signal_id"),
                "reason": reason,
                "timestamp": now_iso
            }

        broker_payload = prep_data["broker_payload"]
        metadata = prep_data["metadata"]
        sig_id = metadata["signal_id"]

        # Phase 1: DRY_RUN / PAPER EXECUTION
        if self.dry_run or self.mode == "PAPER":
            order_id = f"DRY-{now_utc.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
            fill_price = metadata["expected_entry"]

            order_record = {
                "order_id": order_id,
                "signal_id": sig_id,
                "mode": "PAPER_SIMULATED",
                "status": "FILLED",
                "placed_at": now_iso,
                "filled_at": now_iso,
                "broker_payload": broker_payload,
                "fill_price": fill_price,
                "quantity": metadata["quantity"],
                "remaining_quantity": metadata["quantity"],
                "lot_size": metadata["lot_size"],
                "lots_count": metadata["lots_count"],
                "target_1_hit": False,
                "realized_pnl": 0.0,
                "metadata": metadata
            }

            self.placed_orders[order_id] = order_record
            self.signal_to_order[sig_id] = order_id
            self.active_positions[sig_id] = order_record

            self._append_audit_log(order_record)

            logger.success(
                f"📝 [PAPER ORDER FILLED] {sig_id} ➔ {metadata['option_symbol']} | "
                f"Qty: {metadata['quantity']} ({metadata['lots_count']}L) @ ₹{fill_price:.2f} | "
                f"Order ID: {order_id}"
            )

            # Publish event to Redis bus if active
            if self.redis_bus:
                try:
                    await self.redis_bus.publish_signal({
                        "event": "EXECUTION_ORDER_FILLED",
                        "order": order_record
                    })
                except Exception as e:
                    logger.debug(f"Redis publish error in execution bridge: {e}")

            return {
                "status": "SIMULATED_FILLED",
                "order_id": order_id,
                "signal_id": sig_id,
                "fill_price": fill_price,
                "quantity": metadata["quantity"],
                "broker_payload": broker_payload,
                "timestamp": now_iso
            }

        # Real Live Orders (Prohibited in Phase 1)
        raise RuntimeError("Live exchange order execution is strictly locked out in Phase 1.")

    async def handle_partial_exit(self, signal: Dict[str, Any], exit_price: float) -> Dict[str, Any]:
        """
        Handles Target 1 (+1R) execution:
        - 1-Lot Policy: Closes 100% of the position at Target 1 (Full Exit, does not chase Target 2).
        - 2-Lot Policy: Sells 1 lot (50%) at Target 1, moves remaining SL to Breakeven, trails to Target 2.
        """
        sig_id = str(signal.get("signal_id"))
        pos = self.active_positions.get(sig_id)
        if not pos:
            return {"status": "NO_ACTIVE_POSITION", "signal_id": sig_id}

        lot_size = pos["lot_size"]
        rem_qty = pos.get("remaining_quantity", pos.get("quantity", lot_size))
        lots_count = pos.get("lots_count", rem_qty // lot_size)

        # 1-LOT POLICY: 100% Full Exit at Target 1
        if lots_count <= 1 or rem_qty <= lot_size:
            logger.success(
                f"🎯 [1-LOT TARGET 1 FULL EXIT] {sig_id}: 100% position ({rem_qty} qty) exited at Target 1 ₹{exit_price:.2f}! "
                f"Single-lot target achieved."
            )
            return await self.handle_full_exit(
                signal={"signal_id": sig_id},
                exit_price=exit_price,
                reason="TARGET_1_HIT"
            )

        # 2-LOT POLICY: Scale and Trail (50% booked at T1, 50% trailed to T2)
        if pos.get("target_1_hit"):
            return {"status": "ALREADY_PARTIALLY_EXITED", "signal_id": sig_id}

        lot_size = pos["lot_size"]
        entry_price = pos["fill_price"]
        profit_1 = round((exit_price - entry_price) * lot_size, 2)

        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.isoformat()
        exit_order_id = f"DRY-T1-{now_utc.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"

        pos["target_1_hit"] = True
        pos["remaining_quantity"] -= lot_size
        pos["realized_pnl"] += profit_1
        pos["breakeven_sl"] = entry_price

        logger.success(
            f"🎯 [PAPER PARTIAL EXIT T1] {sig_id}: Sold {lot_size} qty @ ₹{exit_price:.2f} | "
            f"Booked: +₹{profit_1:.2f} | Trailing remaining {pos['remaining_quantity']} qty to Breakeven ₹{entry_price:.2f}"
        )

        exit_event = {
            "order_id": exit_order_id,
            "parent_order_id": pos["order_id"],
            "signal_id": sig_id,
            "action": "PARTIAL_EXIT_T1",
            "quantity_sold": lot_size,
            "exit_price": exit_price,
            "realized_pnl": profit_1,
            "remaining_quantity": pos["remaining_quantity"],
            "breakeven_sl": entry_price,
            "timestamp": now_iso
        }
        self._append_audit_log(exit_event)
        return exit_event

    async def handle_full_exit(
        self,
        signal: Dict[str, Any],
        exit_price: float,
        reason: str = "RESOLVED"
    ) -> Dict[str, Any]:
        """
        Simulates complete position closure (Stop Hit, Target 2, or EOD square-off).
        """
        sig_id = str(signal.get("signal_id"))
        pos = self.active_positions.pop(sig_id, None)
        if not pos:
            return {"status": "NO_ACTIVE_POSITION", "signal_id": sig_id}

        rem_qty = pos["remaining_quantity"]
        entry_price = pos["fill_price"]
        leg2_pnl = round((exit_price - entry_price) * rem_qty, 2)
        total_pnl = round(pos["realized_pnl"] + leg2_pnl, 2)

        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.isoformat()
        exit_order_id = f"DRY-EXIT-{now_utc.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"

        pos["status"] = "CLOSED"
        pos["exit_price"] = exit_price
        pos["exit_reason"] = reason
        pos["total_realized_pnl"] = total_pnl
        pos["closed_at"] = now_iso

        # Record trade result in ExecutionRiskGovernor
        if hasattr(self, "risk_governor") and self.risk_governor:
            self.risk_governor.record_trade_result(total_pnl)

        logger.info(
            f"🏁 [PAPER POSITION CLOSED] {sig_id} via {reason} @ ₹{exit_price:.2f} | "
            f"Leg 2 PnL: ₹{leg2_pnl:+.2f} | Total Net Trade PnL: ₹{total_pnl:+.2f}"
        )

        exit_event = {
            "order_id": exit_order_id,
            "parent_order_id": pos["order_id"],
            "signal_id": sig_id,
            "action": "FULL_EXIT",
            "reason": reason,
            "quantity_sold": rem_qty,
            "exit_price": exit_price,
            "total_realized_pnl": total_pnl,
            "timestamp": now_iso
        }
        self._append_audit_log(exit_event)
        return exit_event

    def _append_audit_log(self, record: Dict[str, Any]):
        """Persists order event to daily JSON audit file."""
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filepath = os.path.join(self.audit_dir, f"orders_{today_str}.json")
        try:
            orders = []
            if os.path.exists(filepath):
                with open(filepath, "r", encoding="utf-8") as f:
                    orders = json.load(f)
            orders.append(record)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(orders, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to write execution audit log: {e}")

    def get_audit_history(self) -> List[Dict[str, Any]]:
        """Returns all placed orders in current session."""
        return list(self.placed_orders.values())

    def get_capital_metrics(self) -> Dict[str, Any]:
        """Returns real-time execution capital pool metrics."""
        if hasattr(self, "risk_governor") and self.risk_governor:
            return self.risk_governor.get_capital_metrics(self.active_positions)
        return {
            "total_capital": self.max_capital_per_trade,
            "deployed_capital": 0.0,
            "available_capital": self.max_capital_per_trade,
            "utilization_pct": 0.0,
            "active_positions_count": len(self.active_positions)
        }
