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
import asyncio
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
    Manages order preparation, risk gating, and execution 
    for AngelOne SmartAPI (Phase 1 Paper & Phase 2 Real Trade).
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        auth: Optional[Any] = None,
        redis_bus: Optional[Any] = None,
        force_dry_run: bool = False,
        audit_dir: Optional[str] = None
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

        # Audit directory (allow override for isolated testing)
        self.audit_dir = audit_dir or os.path.join(project_root, self.audit_log_dir)
        os.makedirs(self.audit_dir, exist_ok=True)
        self._load_today_orders()

        logger.info(
            f"🛡️ [EXECUTION BRIDGE] Initialized. Mode: {self.mode} | "
            f"DryRun: {self.dry_run} | Capital Pool: ₹{self.risk_governor.total_capital:,.0f} | "
            f"KillSwitch: {'ACTIVE' if self.kill_switch_active else 'OFF'}"
        )

    def _load_today_orders(self):
        """Restores today's placed orders from JSON audit file if available."""
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filepath = os.path.join(self.audit_dir, f"orders_{today_str}.json")
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    records = json.load(f)
                for r in records:
                    oid = r.get("order_id")
                    action = r.get("action")
                    if action in ("PARTIAL_EXIT_T1", "FULL_EXIT") or (
                        oid and (oid.startswith("DRY-EXIT-") or oid.startswith("DRY-T1-") or 
                                 oid.startswith("LIVE-EXIT-") or oid.startswith("LIVE-T1-"))
                    ):
                        p_oid = r.get("parent_order_id")
                        if p_oid and p_oid in self.placed_orders:
                            self.placed_orders[p_oid]["status"] = "CLOSED"
                            self.placed_orders[p_oid]["exit_price"] = r.get("exit_price")
                            self.placed_orders[p_oid]["exit_reason"] = r.get("reason", "TARGET_HIT")
                            self.placed_orders[p_oid]["total_realized_pnl"] = r.get("total_realized_pnl", 0.0)
                            self.placed_orders[p_oid]["closed_at"] = r.get("timestamp")
                    elif oid:
                        self.placed_orders[oid] = r

                # Restore still-open positions into active_positions & signal_to_order
                for oid, ord_rec in self.placed_orders.items():
                    sig_id = ord_rec.get("signal_id")
                    if sig_id:
                        self.signal_to_order[sig_id] = oid
                    if ord_rec.get("status") == "FILLED":
                        self.active_positions[sig_id] = ord_rec

                total_pnl = sum(float(o.get("total_realized_pnl", 0.0)) for o in self.placed_orders.values())
                if hasattr(self, "risk_governor") and self.risk_governor:
                    self.risk_governor.realized_daily_pnl = round(total_pnl, 2)
                logger.info(f"📁 [EXECUTION BRIDGE] Restored {len(self.placed_orders)} historical session orders ({len(self.active_positions)} active open, PnL: ₹{total_pnl:+.2f}) from {filepath}")
            except Exception as e:
                logger.warning(f"Could not restore session orders from {filepath}: {e}")

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
        if self.force_dry_run:
            self.mode = "PAPER"
            self.dry_run = True
        else:
            self.mode = str(cfg.get("mode", "PAPER")).upper()
            self.dry_run = bool(cfg.get("dry_run", True))
        self.default_product_type = str(cfg.get("default_product_type", "INTRADAY")).upper()
        self.default_order_type = str(cfg.get("default_order_type", "LIMIT")).upper()
        self.slippage_buffer_pct = float(cfg.get("slippage_buffer_pct", 0.5))
        self.limit_timeout_sec = int(cfg.get("limit_timeout_sec", 15))
        self.retry_on_network_failure = bool(cfg.get("retry_on_network_failure", True))
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

    @staticmethod
    def align_tick_size(price: float, tick_size: float = 0.05) -> float:
        """
        Rounds price to nearest valid exchange tick size (0.05 for NSE/BSE options).
        Guarantees strict compliance with exchange tick rules and prevents tick size rejection errors.
        """
        if price <= 0:
            return tick_size
        aligned = round(round(price / tick_size) * tick_size, 2)
        return max(tick_size, aligned)

    def set_execution_mode(self, mode: str, dry_run: Optional[bool] = None) -> Dict[str, Any]:
        """
        Switches execution between Phase 1 (PAPER / DRY_RUN) and Phase 2 (LIVE / REAL TRADE).
        Persists to settings.yaml.
        """
        target = str(mode).strip().upper()
        if target in ("LIVE", "REAL", "PHASE2", "PHASE_2"):
            smart_connect = getattr(self.auth, "smart_connect", None) if self.auth else None
            if not smart_connect:
                logger.warning("⚠️ Switched to LIVE mode, but Angel One SmartAPI is not currently logged in. Ensure session authentication before live ticks arrive.")
            self.mode = "LIVE"
            self.dry_run = False
            self.force_dry_run = False
            logger.warning("🚨 [EXECUTION BRIDGE] Switched to PHASE 2: LIVE REAL BROKER TRADING! Real capital will route to Angel One exchange.")
        elif target in ("PAPER", "DRY_RUN", "DRYRUN", "PHASE1", "PHASE_1"):
            self.mode = "PAPER"
            self.dry_run = True
            logger.info("🛡️ [EXECUTION BRIDGE] Switched to PHASE 1: PAPER / DRY-RUN. Zero financial exchange risk.")
        else:
            raise ValueError(f"Invalid execution mode '{mode}'. Expected 'PAPER' or 'LIVE'.")

        if dry_run is not None:
            self.dry_run = bool(dry_run)

        self.save_config()
        return {
            "status": "SUCCESS",
            "mode": self.mode,
            "dry_run": self.dry_run
        }

    def save_config(self):
        """Persists current runtime execution config to settings.yaml under 'execution' key."""
        try:
            full_cfg = {}
            if os.path.exists(self.config_path):
                with open(self.config_path, "r", encoding="utf-8") as f:
                    full_cfg = yaml.safe_load(f) or {}
            
            exec_cfg = full_cfg.get("execution", {})
            exec_cfg["enabled"] = self.enabled
            exec_cfg["mode"] = self.mode
            exec_cfg["dry_run"] = self.dry_run
            exec_cfg["default_order_type"] = self.default_order_type
            exec_cfg["slippage_buffer_pct"] = float(self.slippage_buffer_pct)
            exec_cfg["limit_timeout_sec"] = int(self.limit_timeout_sec)
            exec_cfg["retry_on_network_failure"] = bool(self.retry_on_network_failure)
            exec_cfg["allowed_strategies"] = list(self.allowed_strategies)
            exec_cfg["max_capital_per_trade"] = float(self.max_capital_per_trade)
            exec_cfg["max_daily_loss"] = float(self.max_daily_loss)
            exec_cfg["nifty_lots"] = int(self.nifty_lots)
            exec_cfg["sensex_lots"] = int(self.sensex_lots)
            if hasattr(self, "risk_governor") and self.risk_governor:
                exec_cfg["total_capital"] = float(self.risk_governor.total_capital)
                exec_cfg["force_single_lot"] = bool(self.risk_governor.force_single_lot)
            
            full_cfg["execution"] = exec_cfg
            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.dump(full_cfg, f, default_flow_style=False, sort_keys=False)
            logger.info(f"💾 [EXECUTION BRIDGE] Configuration successfully saved to {self.config_path}")
        except Exception as e:
            logger.error(f"Failed to persist execution config: {e}")

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
        self.risk_governor.max_capital_per_trade = self.max_capital_per_trade
        self.risk_governor.max_active_positions = self.max_active_orders
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

        # 8. Slippage-Buffered Limit Price with 0.05 Tick Size Alignment
        if self.default_order_type == "MARKET":
            limit_price_str = "0"
            buffered_limit_price = entry_price
        else:
            buffer_multiplier = 1.0 + (self.slippage_buffer_pct / 100.0)
            buffered_limit_price = self.align_tick_size(entry_price * buffer_multiplier, 0.05)
            limit_price_str = f"{buffered_limit_price:.2f}"

        prepared_payload = {
            "variety": "NORMAL",
            "tradingsymbol": str(signal["option_symbol"]),
            "symboltoken": str(signal["option_token"]),
            "transactiontype": "BUY",
            "exchange": exchange,
            "ordertype": self.default_order_type,
            "producttype": self.default_product_type,
            "duration": "DAY",
            "price": limit_price_str,
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
            rejection_record = {
                "signal_id": signal.get("signal_id"),
                "instrument": signal.get("instrument"),
                "option_symbol": signal.get("option_symbol"),
                "action": "ORDER_REJECTED",
                "status": "REJECTED_PRE_TRADE",
                "reason": reason,
                "mode": self.mode,
                "timestamp": now_iso
            }
            self._append_audit_log(rejection_record)
            if self.redis_bus:
                try:
                    await self.redis_bus.publish_signal({
                        "event": "EXECUTION_ORDER_REJECTED",
                        "order": rejection_record
                    })
                except Exception as e:
                    logger.debug(f"Redis publish error on order rejection: {e}")

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

        # Phase 2: REAL LIVE EXCHANGE ORDER EXECUTION
        smart_connect = getattr(self.auth, "smart_connect", None) if self.auth else None
        if not smart_connect and self.auth and hasattr(self.auth, "login"):
            logger.info("⚠️ Angel One SmartAPI session inactive. Attempting automatic re-login...")
            try:
                login_res = await self.auth.login()
                if login_res.get("status"):
                    smart_connect = getattr(self.auth, "smart_connect", None)
                    logger.success("🟢 Angel One SmartAPI session re-established!")
            except Exception as login_err:
                logger.error(f"Automatic session reconnect failed: {login_err}")

        if not smart_connect:
            err_msg = "REJECTED_BROKER_UNAUTHENTICATED (Angel One SmartAPI session is inactive / login required)"
            logger.critical(f"❌ [LIVE EXECUTION FAILED] {sig_id}: {err_msg}")
            fail_record = {
                "signal_id": sig_id,
                "instrument": metadata.get("instrument"),
                "option_symbol": metadata.get("option_symbol"),
                "action": "ORDER_REJECTED",
                "status": "UNPLACED_AUTH_FAIL",
                "reason": err_msg,
                "mode": self.mode,
                "timestamp": now_iso
            }
            self._append_audit_log(fail_record)
            if self.redis_bus:
                try:
                    await self.redis_bus.publish_signal({
                        "event": "EXECUTION_ORDER_REJECTED",
                        "order": fail_record
                    })
                except Exception as e:
                    logger.debug(f"Redis publish error: {e}")

            return {
                "status": "REJECTED",
                "signal_id": sig_id,
                "reason": err_msg,
                "timestamp": now_iso
            }

        try:
            loop = asyncio.get_running_loop()
            logger.info(
                f"🚀 [ROUTING LIVE ORDER] Sending {metadata['option_symbol']} "
                f"({metadata['quantity']} qty @ {broker_payload['ordertype']} ₹{broker_payload['price']}) to {broker_payload['exchange']}..."
            )
            raw_resp = await loop.run_in_executor(None, lambda: smart_connect.placeOrder(broker_payload))

            order_id = None
            if isinstance(raw_resp, dict):
                if raw_resp.get("status") and raw_resp.get("data", {}).get("orderid"):
                    order_id = str(raw_resp["data"]["orderid"])
                elif not raw_resp.get("status"):
                    err_reason = raw_resp.get("message") or raw_resp.get("errorcode") or "BROKER_RMS_REJECTED"
                    logger.error(f"❌ [BROKER REJECTION] {sig_id}: {err_reason}")
                    fail_record = {
                        "signal_id": sig_id,
                        "instrument": metadata.get("instrument"),
                        "option_symbol": metadata.get("option_symbol"),
                        "action": "ORDER_REJECTED",
                        "status": "REJECTED_BY_BROKER",
                        "reason": f"BROKER_ERROR: {err_reason}",
                        "broker_payload": broker_payload,
                        "mode": self.mode,
                        "timestamp": now_iso
                    }
                    self._append_audit_log(fail_record)
                    if self.redis_bus:
                        try:
                            await self.redis_bus.publish_signal({
                                "event": "EXECUTION_ORDER_REJECTED",
                                "order": fail_record
                            })
                        except Exception:
                            pass
                    return {
                        "status": "REJECTED",
                        "signal_id": sig_id,
                        "reason": f"BROKER_ERROR: {err_reason}",
                        "timestamp": now_iso
                    }
            elif raw_resp:
                order_id = str(raw_resp)

            if not order_id:
                raise ValueError(f"Invalid order response received from Angel One: {raw_resp}")

            fill_price = metadata["expected_entry"]
            order_record = {
                "order_id": order_id,
                "signal_id": sig_id,
                "mode": "LIVE_EXCHANGE",
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
                f"🚀 [LIVE REAL ORDER PLACED & RECORDED] {sig_id} ➔ {metadata['option_symbol']} | "
                f"Qty: {metadata['quantity']} ({metadata['lots_count']}L) @ ₹{fill_price:.2f} | "
                f"Broker Order ID: {order_id}"
            )

            if self.redis_bus:
                try:
                    await self.redis_bus.publish_signal({
                        "event": "EXECUTION_ORDER_FILLED",
                        "order": order_record
                    })
                except Exception as e:
                    logger.debug(f"Redis publish error in execution bridge: {e}")

            return {
                "status": "LIVE_FILLED",
                "order_id": order_id,
                "signal_id": sig_id,
                "fill_price": fill_price,
                "quantity": metadata["quantity"],
                "broker_payload": broker_payload,
                "timestamp": now_iso
            }

        except Exception as e:
            logger.exception(f"❌ [LIVE ORDER EXCEPTION] Failed to place order for {sig_id}: {e}")
            fail_record = {
                "signal_id": sig_id,
                "instrument": metadata.get("instrument"),
                "option_symbol": metadata.get("option_symbol"),
                "action": "ORDER_REJECTED",
                "status": "BROKER_EXCEPTION",
                "reason": f"BROKER_EXCEPTION: {str(e)}",
                "broker_payload": broker_payload,
                "mode": self.mode,
                "timestamp": now_iso
            }
            self._append_audit_log(fail_record)
            if self.redis_bus:
                try:
                    await self.redis_bus.publish_signal({
                        "event": "EXECUTION_ORDER_REJECTED",
                        "order": fail_record
                    })
                except Exception:
                    pass
            return {
                "status": "REJECTED",
                "signal_id": sig_id,
                "reason": f"BROKER_EXCEPTION: {str(e)}",
                "timestamp": now_iso
            }

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
        is_live_pos = (pos.get("mode") == "LIVE_EXCHANGE" and not self.dry_run)
        exit_order_id = None

        if is_live_pos:
            smart_connect = getattr(self.auth, "smart_connect", None) if self.auth else None
            if smart_connect:
                try:
                    if self.default_order_type == "MARKET":
                        sell_price_str = "0"
                    else:
                        buffer_multiplier = 1.0 - (self.slippage_buffer_pct / 100.0)
                        sell_price_str = f"{self.align_tick_size(exit_price * buffer_multiplier, 0.05):.2f}"
                    sell_payload = {
                        "variety": "NORMAL",
                        "tradingsymbol": str(pos.get("broker_payload", {}).get("tradingsymbol", pos.get("metadata", {}).get("option_symbol"))),
                        "symboltoken": str(pos.get("broker_payload", {}).get("symboltoken", pos.get("metadata", {}).get("option_token"))),
                        "transactiontype": "SELL",
                        "exchange": str(pos.get("broker_payload", {}).get("exchange", "NFO")),
                        "ordertype": self.default_order_type,
                        "producttype": self.default_product_type,
                        "duration": "DAY",
                        "price": sell_price_str,
                        "quantity": str(lot_size)
                    }
                    loop = asyncio.get_running_loop()
                    raw_resp = await loop.run_in_executor(None, lambda: smart_connect.placeOrder(sell_payload))
                    if isinstance(raw_resp, dict) and raw_resp.get("data", {}).get("orderid"):
                        exit_order_id = str(raw_resp["data"]["orderid"])
                    elif raw_resp:
                        exit_order_id = str(raw_resp)
                    logger.success(f"🚀 [LIVE PARTIAL EXIT T1] Broker Order ID: {exit_order_id}")
                except Exception as e:
                    logger.error(f"❌ Failed to route live partial exit order to broker: {e}")

        if not exit_order_id:
            exit_order_id = f"{'LIVE' if is_live_pos else 'DRY'}-T1-{now_utc.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"

        pos["target_1_hit"] = True
        pos["remaining_quantity"] -= lot_size
        pos["realized_pnl"] += profit_1
        pos["breakeven_sl"] = entry_price

        log_tag = "LIVE" if is_live_pos else "PAPER"
        logger.success(
            f"🎯 [{log_tag} PARTIAL EXIT T1] {sig_id}: Sold {lot_size} qty @ ₹{exit_price:.2f} | "
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
        Executes complete position closure (Stop Hit, Target 2, or EOD square-off).
        Routes real SELL order to broker if position was placed in LIVE mode.
        If live exit fails, preserves position in active_positions for safety.
        """
        sig_id = str(signal.get("signal_id"))
        pos = self.active_positions.get(sig_id)
        if not pos:
            return {"status": "NO_ACTIVE_POSITION", "signal_id": sig_id}

        rem_qty = pos["remaining_quantity"]
        entry_price = pos["fill_price"]
        leg2_pnl = round((exit_price - entry_price) * rem_qty, 2)
        total_pnl = round(pos["realized_pnl"] + leg2_pnl, 2)

        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.isoformat()
        is_live_pos = (pos.get("mode") == "LIVE_EXCHANGE" and not self.dry_run)
        exit_order_id = None
        exit_failed = False
        exit_err_msg = ""

        if is_live_pos:
            smart_connect = getattr(self.auth, "smart_connect", None) if self.auth else None
            if not smart_connect:
                exit_failed = True
                exit_err_msg = "Broker SmartAPI session inactive during exit order placement"
            else:
                try:
                    if self.default_order_type == "MARKET":
                        sell_price_str = "0"
                    else:
                        buffer_multiplier = 1.0 - (self.slippage_buffer_pct / 100.0)
                        sell_price_str = f"{self.align_tick_size(exit_price * buffer_multiplier, 0.05):.2f}"
                    sell_payload = {
                        "variety": "NORMAL",
                        "tradingsymbol": str(pos.get("broker_payload", {}).get("tradingsymbol", pos.get("metadata", {}).get("option_symbol"))),
                        "symboltoken": str(pos.get("broker_payload", {}).get("symboltoken", pos.get("metadata", {}).get("option_token"))),
                        "transactiontype": "SELL",
                        "exchange": str(pos.get("broker_payload", {}).get("exchange", "NFO")),
                        "ordertype": self.default_order_type,
                        "producttype": self.default_product_type,
                        "duration": "DAY",
                        "price": sell_price_str,
                        "quantity": str(rem_qty)
                    }
                    loop = asyncio.get_running_loop()
                    raw_resp = await loop.run_in_executor(None, lambda: smart_connect.placeOrder(sell_payload))
                    if isinstance(raw_resp, dict):
                        if raw_resp.get("status") and raw_resp.get("data", {}).get("orderid"):
                            exit_order_id = str(raw_resp["data"]["orderid"])
                        elif not raw_resp.get("status"):
                            exit_failed = True
                            exit_err_msg = raw_resp.get("message") or raw_resp.get("errorcode") or "BROKER_RMS_EXIT_REJECTED"
                    elif raw_resp:
                        exit_order_id = str(raw_resp)
                    if exit_order_id:
                        logger.success(f"🚀 [LIVE FULL EXIT] Broker Order ID: {exit_order_id}")
                except Exception as e:
                    exit_failed = True
                    exit_err_msg = f"BROKER_EXCEPTION: {str(e)}"
                    logger.error(f"❌ Failed to route live full exit order to broker: {e}")

        if exit_failed:
            pos["exit_failed"] = True
            pos["last_exit_error"] = exit_err_msg
            logger.critical(f"🚨 [CRITICAL: LIVE EXIT ORDER FAILED] {sig_id}: {exit_err_msg}. Position preserved in active positions for manual retry!")
            fail_event = {
                "signal_id": sig_id,
                "action": "EXIT_FAILED",
                "reason": exit_err_msg,
                "timestamp": now_iso
            }
            self._append_audit_log(fail_event)
            if self.redis_bus:
                try:
                    await self.redis_bus.publish_signal({
                        "event": "EXECUTION_EXIT_FAILED",
                        "signal_id": sig_id,
                        "option_symbol": pos.get("metadata", {}).get("option_symbol"),
                        "reason": exit_err_msg
                    })
                except Exception:
                    pass
            return {
                "status": "EXIT_FAILED",
                "signal_id": sig_id,
                "reason": exit_err_msg,
                "timestamp": now_iso
            }

        # Successful exit: Now remove from active_positions
        self.active_positions.pop(sig_id, None)

        if not exit_order_id:
            exit_order_id = f"{'LIVE' if is_live_pos else 'DRY'}-EXIT-{now_utc.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"

        pos["status"] = "CLOSED"
        pos["exit_price"] = exit_price
        pos["exit_reason"] = reason
        pos["total_realized_pnl"] = total_pnl
        pos["closed_at"] = now_iso

        # Record trade result in ExecutionRiskGovernor
        if hasattr(self, "risk_governor") and self.risk_governor:
            self.risk_governor.record_trade_result(total_pnl)

        log_tag = "LIVE" if is_live_pos else "PAPER"
        logger.info(
            f"🏁 [{log_tag} POSITION CLOSED] {sig_id} via {reason} @ ₹{exit_price:.2f} | "
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
        """Persists order event to daily JSON audit file with atomic write protection."""
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filepath = os.path.join(self.audit_dir, f"orders_{today_str}.json")
        temp_filepath = f"{filepath}.tmp"
        try:
            orders = []
            if os.path.exists(filepath):
                with open(filepath, "r", encoding="utf-8") as f:
                    orders = json.load(f)
            orders.append(record)
            with open(temp_filepath, "w", encoding="utf-8") as f:
                json.dump(orders, f, indent=2)
            os.replace(temp_filepath, filepath)
        except Exception as e:
            logger.error(f"Failed to write execution audit log: {e}")
            if os.path.exists(temp_filepath):
                try:
                    os.remove(temp_filepath)
                except Exception:
                    pass

    def get_today_orders(self) -> List[Dict[str, Any]]:
        """Returns all order and rejection audit records recorded today."""
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filepath = os.path.join(self.audit_dir, f"orders_{today_str}.json")
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error reading today's audit file {filepath}: {e}")
        return list(self.placed_orders.values())

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
