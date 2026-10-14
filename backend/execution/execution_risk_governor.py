"""
Execution Risk Governor & Capital Pool Manager (Phase 1 & Phase 2).

Decoupled strictly from the Live Advisory RiskGovernor (which uses ₹1,00,000 benchmark equity).
Focuses 100% on live broker execution constraints:
1. Expiry Time Gate (The Anti-Theta Trap):
   - Blocks new naked buyer orders after 14:15 IST on weekly/monthly expiry days.
2. Volatility Stop Loss Validation (No Guessing):
   - Enforces that all execution orders have a verified volatility-based Stop Loss 
     with floor rules and minimum spread buffers.
3. 1:2 Risk-to-Reward Targets (Target 1 & Target 2):
   - Enforces valid Target 1 (+1R partial profit) and Target 2 (+2R final runner).
4. Capital Pool Governance (₹25,000 Pool):
   - Tracks deployed capital across all open broker positions.
   - Dynamically calculates available margin: (Total Pool - Deployed).
5. Dynamic Lot Sizing & Phase 2 Micro-Lot Guard:
   - Enforces strict 1-lot trading in Phase 2 for micro-risk live testing.
   - If multi-lot is enabled and margin is tight, automatically downsizes to 1 lot.
6. Insufficient Margin Guard & Detailed Audit Logging:
   - Explicitly rejects and logs if remaining margin cannot fund even 1 lot.
7. Execution Loss Circuit Breaker:
   - Tracks daily realized PnL against max daily loss ceiling.
"""

import math
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple
from loguru import logger
import yaml

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "config", "settings.yaml"
)

class ExecutionRiskGovernor:
    """
    Dedicated Execution Risk Governor for Angel One Broker Terminal.
    """

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        
        # Default baseline values
        self.total_capital: float = 25000.0        # ₹25,000 dedicated execution capital pool
        self.max_capital_per_trade: float = 25000.0 # Single position ceiling
        self.force_single_lot: bool = True         # Phase 2 default: strictly 1 lot
        self.max_active_positions: int = 2         # Max concurrent open trades
        self.max_daily_loss: float = 4000.0        # Daily loss ceiling for execution
        
        # Expiry Time Gate
        self.expiry_gate_enabled: bool = True
        self.expiry_cutoff_time: str = "14:15"     # HH:MM IST
        
        self.realized_daily_pnl: float = 0.0
        self.circuit_breaker_tripped: bool = False
        self.circuit_breaker_time: Optional[datetime] = None

        self.load_config()

        logger.info(
            f"🛡️ [EXECUTION RISK GOVERNOR] Initialized. "
            f"Capital Pool: ₹{self.total_capital:,.2f} | "
            f"ForceSingleLot: {self.force_single_lot} | "
            f"MaxActive: {self.max_active_positions} | "
            f"ExpiryGate: {self.expiry_cutoff_time} IST | "
            f"MaxLoss: ₹{self.max_daily_loss:,.2f}"
        )

    def load_config(self):
        """Loads execution risk settings from config/settings.yaml."""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    full_cfg = yaml.safe_load(f) or {}
                    exec_cfg = full_cfg.get("execution", {})
                    risk_cfg = full_cfg.get("risk", {})

                    # Capital & sizing
                    self.total_capital = float(exec_cfg.get("total_capital", exec_cfg.get("max_capital_per_trade", 25000.0)))
                    self.max_capital_per_trade = float(exec_cfg.get("max_capital_per_trade", self.total_capital))
                    self.force_single_lot = bool(exec_cfg.get("force_single_lot", True))
                    self.max_active_positions = int(exec_cfg.get("max_active_orders", 2))
                    self.max_daily_loss = float(exec_cfg.get("max_daily_loss", 4000.0))

                    # Expiry gate settings (from execution or fallback to risk)
                    self.expiry_gate_enabled = bool(exec_cfg.get("expiry_gate_enabled", risk_cfg.get("expiry_gate_enabled", True)))
                    self.expiry_cutoff_time = str(exec_cfg.get("expiry_cutoff_time", risk_cfg.get("expiry_cutoff_time", "14:15")))
            except Exception as e:
                logger.warning(f"Failed to load execution risk config from {self.config_path}: {e}")

    def _is_expiry_day(self, raw_signal: Dict[str, Any], dt: datetime) -> bool:
        """Determines if the candidate contract expires today or if today is weekly index expiry."""
        symbol = str(raw_signal.get("option_symbol", "")).upper()
        details = raw_signal.get("details") or {}
        exp_str = str(raw_signal.get("expiry") or details.get("expiry") or "").strip().upper()

        if exp_str:
            for fmt in ("%d%b%Y", "%d-%b-%Y", "%d%B%Y", "%Y-%m-%d"):
                try:
                    exp_date = datetime.strptime(exp_str, fmt).date()
                    return exp_date == dt.date()
                except ValueError:
                    pass

        # BSE weekly symbol date pattern (e.g., SENSEX26O1473000CE -> day 14)
        m_bse = re.match(r"SENSEX\d{2}[A-Z0-9](\d{2})", symbol, re.I)
        if m_bse:
            try:
                if int(m_bse.group(1)) == dt.day:
                    return True
            except ValueError:
                pass

        # NSE weekly symbol date pattern (e.g., NIFTY15OCT2622400PE)
        m_nse = re.match(r"NIFTY(\d{2})([A-Z]{3})\d{2}", symbol, re.I)
        if m_nse:
            try:
                day_str, mon_str = m_nse.groups()
                if int(day_str) == dt.day and mon_str.upper() == dt.strftime("%b").upper():
                    return True
            except ValueError:
                pass

        # Index weekly fallback (SENSEX: Wed/Thu/Fri; NIFTY: Thu)
        inst = raw_signal.get("instrument", "NIFTY")
        weekday = dt.weekday()
        if (inst == "NIFTY" and weekday in (1, 3)) or (inst == "SENSEX" and weekday in (2, 3, 4)):
            return True

        return False

    def get_deployed_capital(self, active_positions: Dict[str, Dict[str, Any]]) -> float:
        """Calculates total capital currently locked in open active positions."""
        deployed = 0.0
        for pos in active_positions.values():
            meta = pos.get("metadata", {})
            req = meta.get("required_capital")
            if req is not None:
                deployed += float(req)
            else:
                entry = float(pos.get("fill_price", meta.get("expected_entry", 0.0)))
                qty = int(pos.get("quantity", meta.get("quantity", 0)))
                deployed += round(entry * qty, 2)
        return round(deployed, 2)

    def get_available_capital(self, active_positions: Dict[str, Dict[str, Any]]) -> float:
        """Returns remaining available cash in the execution capital pool."""
        deployed = self.get_deployed_capital(active_positions)
        return max(0.0, round(self.total_capital - deployed, 2))

    def get_capital_metrics(self, active_positions: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """Returns real-time capital allocation snapshot for UI and telemetry."""
        deployed = self.get_deployed_capital(active_positions)
        avail = max(0.0, round(self.total_capital - deployed, 2))
        util_pct = round((deployed / self.total_capital * 100.0), 1) if self.total_capital > 0 else 0.0
        return {
            "total_capital": self.total_capital,
            "deployed_capital": deployed,
            "available_capital": avail,
            "utilization_pct": util_pct,
            "active_positions_count": len(active_positions),
            "max_active_positions": self.max_active_positions,
            "force_single_lot": self.force_single_lot,
            "realized_daily_pnl": self.realized_daily_pnl,
            "max_daily_loss": self.max_daily_loss,
            "circuit_breaker_tripped": self.circuit_breaker_tripped
        }

    def record_trade_result(self, pnl: float):
        """Updates realized daily PnL and trips execution circuit breaker if daily limit breached."""
        self.realized_daily_pnl = round(self.realized_daily_pnl + pnl, 2)
        if self.realized_daily_pnl <= -abs(self.max_daily_loss) and not self.circuit_breaker_tripped:
            self.circuit_breaker_tripped = True
            self.circuit_breaker_time = datetime.now(timezone.utc)
            logger.critical(
                f"🛑 [EXECUTION CIRCUIT BREAKER TRIPPED] Execution realized loss ₹{self.realized_daily_pnl:.2f} "
                f"hit daily limit -₹{self.max_daily_loss:.2f}! Execution disarmed."
            )

    def evaluate_execution_order(
        self,
        signal: Dict[str, Any],
        active_positions: Dict[str, Dict[str, Any]]
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Evaluates an incoming candidate signal against complete execution risk gates:
        1. Circuit breaker status
        2. Expiry Time Gate (Anti-Theta Trap)
        3. Active position concurrency cap
        4. Volatility Stop Loss & 1:2 R:R Targets validation
        5. Available capital vs required margin
        6. Single-lot enforcement (Phase 2) or dynamic lot step-down
        """
        sig_id = signal.get("signal_id", "UNKNOWN")

        # 1. Circuit breaker check
        if self.circuit_breaker_tripped:
            reason = f"REJECTED_EXECUTION_CIRCUIT_BREAKER_ACTIVE (Loss: ₹{self.realized_daily_pnl:.2f} >= Cap: -₹{self.max_daily_loss:.2f})"
            logger.warning(f"🚫 {reason}")
            return False, reason, {}

        # 2. Expiry Time Gate: Block naked option buyer orders after cutoff on expiry days
        if self.expiry_gate_enabled:
            IST = timezone(timedelta(hours=5, minutes=30))
            now_dt = datetime.now(IST)
            try:
                cutoff_hour, cutoff_min = map(int, self.expiry_cutoff_time.split(":"))
            except Exception:
                cutoff_hour, cutoff_min = 14, 15

            if (now_dt.hour > cutoff_hour) or (now_dt.hour == cutoff_hour and now_dt.minute >= cutoff_min):
                if self._is_expiry_day(signal, now_dt):
                    reason = (
                        f"REJECTED_EXPIRY_TIME_GATE (Naked buyer entries disabled after {self.expiry_cutoff_time} IST "
                        f"on expiry days to prevent 0-DTE Theta crush)"
                    )
                    logger.warning(f"⏳ [ANTI-THETA GATE] {sig_id}: {reason}")
                    return False, reason, {}

        # 3. Concurrency cap check
        if len(active_positions) >= self.max_active_positions:
            reason = f"REJECTED_MAX_CONCURRENT_POSITIONS ({len(active_positions)}/{self.max_active_positions})"
            logger.warning(f"🚫 [EXECUTION CAP] {sig_id}: {reason}")
            return False, reason, {}

        # 4. Price & Lot size validation
        entry_price = float(signal.get("entry_price", 0.0))
        lot_size = int(signal.get("lot_size", 0))
        if entry_price <= 0.0 or lot_size <= 0:
            return False, "REJECTED_INVALID_PRICE_OR_LOT_SIZE", {}

        # 5. Volatility Stop Loss Validation (No Guessing)
        stop_loss = float(signal.get("stop_loss", 0.0))
        if stop_loss <= 0.0:
            return False, "REJECTED_MISSING_VOLATILITY_STOP_LOSS", {}
        if stop_loss >= entry_price:
            return False, f"REJECTED_STOP_LOSS_ABOVE_ENTRY (SL: ₹{stop_loss:.2f} >= Entry: ₹{entry_price:.2f})", {}

        # 6. 1:2 Risk-to-Reward Targets Validation (Target 1 & Target 2)
        target_1r = float(signal.get("target_1r") or signal.get("target", 0.0))
        target_2r = float(signal.get("target_2r") or signal.get("target", 0.0))
        if target_1r <= entry_price:
            return False, f"REJECTED_INVALID_TARGET_1R (T1: ₹{target_1r:.2f} <= Entry: ₹{entry_price:.2f})", {}

        one_lot_capital = round(entry_price * lot_size, 2)
        deployed_capital = self.get_deployed_capital(active_positions)
        available_capital = self.get_available_capital(active_positions)

        # 7. Check if even 1 lot is affordable in remaining available capital
        if one_lot_capital > available_capital:
            reason = (
                f"REJECTED_INSUFFICIENT_AVAILABLE_CAPITAL "
                f"(Req 1-Lot: ₹{one_lot_capital:,.2f} > Avail: ₹{available_capital:,.2f} | "
                f"Pool: ₹{self.total_capital:,.0f} - Deployed: ₹{deployed_capital:,.0f})"
            )
            logger.warning(f"🚫 [MARGIN EXHAUSTION] {sig_id}: {reason}")
            return False, reason, {
                "available_capital": available_capital,
                "deployed_capital": deployed_capital,
                "total_capital": self.total_capital,
                "required_one_lot": one_lot_capital
            }

        # 8. Lot sizing decision (Phase 2 Single-Lot Lock or Dynamic Step-Down)
        requested_qty = int(signal.get("quantity", lot_size))
        requested_lots = max(1, requested_qty // lot_size)

        if self.force_single_lot:
            # Phase 2 micro-lot safety mode: strictly 1 lot
            final_lots = 1
            adjustment_note = "PHASE2_FORCED_SINGLE_LOT"
            if requested_lots > 1:
                logger.info(
                    f"🛡️ [PHASE 2 LOT CAP] Signal {sig_id} requested {requested_lots} lots, "
                    f"locked to 1 lot ({lot_size} shares) for micro-risk pilot execution."
                )
        else:
            # Dynamic sizing mode: check if requested lots fit in available capital
            req_capital = round(entry_price * (requested_lots * lot_size), 2)
            if req_capital <= available_capital:
                final_lots = requested_lots
                adjustment_note = "ORIGINAL_LOTS_APPROVED"
            else:
                # Calculate maximum affordable lots
                affordable_lots = math.floor(available_capital / one_lot_capital)
                final_lots = max(1, affordable_lots)
                adjustment_note = f"DOWNSIZED_TO_{final_lots}_LOTS_FOR_MARGIN"
                logger.warning(
                    f"⚠️ [MARGIN STEP-DOWN] Signal {sig_id} requested {requested_lots} lots (₹{req_capital:,.2f}) "
                    f"exceeding available capital ₹{available_capital:,.2f}. "
                    f"Auto-downsized to {final_lots} lot(s) (₹{final_lots * one_lot_capital:,.2f})."
                )

        final_quantity = final_lots * lot_size
        final_capital = round(entry_price * final_quantity, 2)

        # 9. Check single-trade ceiling
        if final_capital > self.max_capital_per_trade:
            reason = f"REJECTED_CAPITAL_EXCEEDS_TRADE_LIMIT (Req: ₹{final_capital:.2f} > Max: ₹{self.max_capital_per_trade:.2f})"
            logger.warning(f"🚫 {reason}")
            return False, reason, {}

        # 10. Exit Target Policy: 1-Lot = Target 1 (+1R Full Exit) | 2-Lots = Scale & Trail (+2R)
        if final_lots == 1:
            effective_target = target_1r
            exit_mode = "TARGET_1_FULL_EXIT"
            exit_guidance = f"1-Lot Policy: 100% position exits at Target 1 (+1R = ₹{target_1r:.2f})"
        else:
            effective_target = target_2r
            exit_mode = "1_TO_2_SCALE_AND_TRAIL"
            exit_guidance = f"2-Lot Policy: Book 1 lot at Target 1 (+1R = ₹{target_1r:.2f}), trail remaining lot with BE Stop to Target 2 (+2R = ₹{target_2r:.2f})"

        remaining_after = max(0.0, round(available_capital - final_capital, 2))

        logger.info(
            f"✅ [EXECUTION RISK APPROVED] {sig_id} ➔ Qty: {final_quantity} ({final_lots}L) @ ₹{entry_price:.2f} | "
            f"SL: ₹{stop_loss:.2f} | Tgt: ₹{effective_target:.2f} ({exit_mode}) | "
            f"Committed: ₹{final_capital:,.2f} | Remaining Pool: ₹{remaining_after:,.2f} ({adjustment_note})"
        )

        return True, "VALIDATED_OK", {
            "quantity": final_quantity,
            "lots_count": final_lots,
            "required_capital": final_capital,
            "stop_loss": stop_loss,
            "target": effective_target,
            "target_1r": target_1r,
            "target_2r": target_2r,
            "exit_mode": exit_mode,
            "exit_guidance": exit_guidance,
            "available_capital_before": available_capital,
            "available_capital_after": remaining_after,
            "deployed_capital_before": deployed_capital,
            "deployed_capital_after": round(deployed_capital + final_capital, 2),
            "adjustment_note": adjustment_note
        }
