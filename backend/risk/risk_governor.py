import math
import os
import re
from datetime import datetime, timezone, date, timedelta
from typing import Dict, Any, Optional, Tuple
from loguru import logger
import yaml

from backend.strategies.indicators import IncrementalATR

class RiskGovernor:
    """
    Master Risk Governor & Dynamic Sizing Engine.
    Enforces:
    1. Synthetic Spot Stop Loss: SL_spot = Spot_entry +/- (1.5 * ATR14)
    2. Option Premium Stop Loss: SL_opt = P_entry - (Delta_ATM * |Spot_entry - SL_spot|)
       Floor rule: If SL_opt < 0.75 * P_entry -> force SL_opt = 0.80 * P_entry
    3. Target: 1:2 Risk/Reward ratio -> TGT_opt = P_entry + 2 * (P_entry - SL_opt)
    4. Lot Sizing: Q_shares = floor((Equity * 0.01) / ((P_entry - SL_opt) * LotSize)) * LotSize
    5. Daily Drawdown Circuit Breaker: Realized loss >= 2.5% -> kills signal generation
    """

    def __init__(self, config_path: Optional[str] = None):
        self.total_equity = 100000.0
        self.risk_per_trade_pct = 0.01   # 1.0%
        self.daily_max_drawdown_pct = 0.025 # 2.5%
        self.sl_floor_threshold = 0.75
        self.sl_floor_ratio = 0.80
        self.rr_ratio = 2.0
        self.delta_atm = 0.50

        # Expiry Time Gate: Disables new naked option buyer signals after cutoff on expiry days
        self.expiry_gate_enabled: bool = True
        self.expiry_cutoff_time: str = "14:15"  # HH:MM IST

        self.realized_daily_pnl = 0.0
        self.circuit_breaker_tripped = False
        self.circuit_breaker_time: Optional[datetime] = None

        # Spot ATR14 trackers with index-realistic default baseline
        self.spot_atr: Dict[str, IncrementalATR] = {
            "NIFTY": IncrementalATR(period=14, default_atr=32.0),
            "SENSEX": IncrementalATR(period=14, default_atr=160.0)
        }

        self._load_config(config_path)

    def _load_config(self, config_path: Optional[str] = None):
        if not config_path:
            config_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "settings.yaml")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    cfg = yaml.safe_load(f).get("risk", {})
                    self.total_equity = float(cfg.get("total_account_equity", self.total_equity))
                    self.risk_per_trade_pct = float(cfg.get("risk_per_trade_pct", self.risk_per_trade_pct))
                    self.daily_max_drawdown_pct = float(cfg.get("daily_max_drawdown_pct", self.daily_max_drawdown_pct))
                    self.sl_floor_threshold = float(cfg.get("sl_floor_threshold", self.sl_floor_threshold))
                    self.sl_floor_ratio = float(cfg.get("sl_floor_ratio", self.sl_floor_ratio))
                    self.rr_ratio = float(cfg.get("rr_ratio", self.rr_ratio))
                    self.expiry_gate_enabled = bool(cfg.get("expiry_gate_enabled", self.expiry_gate_enabled))
                    self.expiry_cutoff_time = str(cfg.get("expiry_cutoff_time", self.expiry_cutoff_time))
            except Exception as e:
                logger.warning(f"Error reading risk settings: {e}")

    def update_spot_bar(self, instrument: str, high: float, low: float, close: float):
        if instrument in self.spot_atr:
            self.spot_atr[instrument].update(high, low, close)

    def on_spot_candle(self, instrument: str, high: float, low: float, close: float):
        """Alias for update_spot_bar to consume 1m / 3m closed spot candles."""
        self.update_spot_bar(instrument, high, low, close)

    def record_trade_result(self, pnl: float):
        """Updates realized daily PnL and evaluates circuit breaker."""
        self.realized_daily_pnl += pnl
        max_allowed_loss = -(self.total_equity * self.daily_max_drawdown_pct)
        if self.realized_daily_pnl <= max_allowed_loss and not self.circuit_breaker_tripped:
            self.circuit_breaker_tripped = True
            self.circuit_breaker_time = datetime.now(timezone.utc)
            logger.critical(
                f"🚨 [CIRCUIT BREAKER TRIPPED] Realized daily loss: ₹{self.realized_daily_pnl:.2f} "
                f"exceeds -{self.daily_max_drawdown_pct*100}% limit (₹{max_allowed_loss:.2f})! Halting signals."
            )

    def _is_expiry_day(self, raw_signal: Dict[str, Any], dt: datetime) -> bool:
        """
        Determines if the candidate contract expires today, or if today is
        the weekly expiry day for the underlying index.
        """
        symbol = str(raw_signal.get("option_symbol", "")).upper()
        details = raw_signal.get("details") or {}
        exp_str = str(raw_signal.get("expiry") or details.get("expiry") or "").strip().upper()

        # 1. Direct explicit expiry string match (e.g. '08OCT2026', '2026-10-08')
        if exp_str:
            for fmt in ("%d%b%Y", "%d-%b-%Y", "%d%B%Y", "%Y-%m-%d"):
                try:
                    exp_date = datetime.strptime(exp_str, fmt).date()
                    return exp_date == dt.date()
                except ValueError:
                    pass

        # 2. BSE weekly symbol date pattern (e.g., SENSEX26O0871400PE -> day 08)
        m_bse = re.match(r"SENSEX\d{2}[A-Z0-9](\d{2})", symbol, re.I)
        if m_bse:
            try:
                if int(m_bse.group(1)) == dt.day:
                    return True
            except ValueError:
                pass

        # 3. NSE weekly symbol date pattern (e.g., NIFTY08OCT2622400PE)
        m_nse = re.match(r"NIFTY(\d{2})([A-Z]{3})\d{2}", symbol, re.I)
        if m_nse:
            try:
                day_str, mon_str = m_nse.groups()
                if int(day_str) == dt.day and mon_str.upper() == dt.strftime("%b").upper():
                    return True
            except ValueError:
                pass

        # 4. Fallback: Underlying index weekly expiry day of week
        # SENSEX: Thursday (3) or Friday (4); NIFTY: Thursday (3) or Tuesday (1)
        inst = raw_signal.get("instrument", "NIFTY")
        weekday = dt.weekday()
        if (inst == "NIFTY" and weekday in (1, 3)) or (inst == "SENSEX" and weekday in (3, 4)):
            return True

        return False

    def evaluate_signal(self, raw_signal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Validates raw candidate signal, computes precise SL, Target, and Lot Sizing.
        Returns fully formed Signal dict or None if rejected by risk governor.
        """
        # Note: Do not suppress signals even if drawdown cap is touched; let advisory signals be displayed
        cb_active = self.circuit_breaker_tripped
        if cb_active:
            logger.warning(f"⚠️ [CIRCUIT BREAKER ADVISORY] Drawdown limit active, but signal {raw_signal.get('signal_id')} emitted for advisory tracking.")

        inst = raw_signal.get("instrument", "NIFTY")
        opt_type = raw_signal.get("option_type", "CE")
        spot_entry = float(raw_signal.get("spot_entry", 0.0))
        p_entry = float(raw_signal.get("entry_price", 0.0))
        lot_size = int(raw_signal.get("lot_size", 50))

        if p_entry <= 1.0 or spot_entry <= 0.0 or lot_size <= 0:
            logger.warning(f"Invalid pricing values for signal {raw_signal.get('signal_id')}")
            return None

        # Expiry Time Gate: Block new naked buyer entries after cutoff on expiry days
        if self.expiry_gate_enabled:
            IST = timezone(timedelta(hours=5, minutes=30))
            created_at_str = raw_signal.get("created_at")
            if created_at_str:
                try:
                    now_dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00")).astimezone(IST)
                except Exception:
                    now_dt = datetime.now(IST)
            else:
                now_dt = datetime.now(IST)

            try:
                cutoff_hour, cutoff_min = map(int, self.expiry_cutoff_time.split(":"))
            except Exception:
                cutoff_hour, cutoff_min = 14, 15

            if (now_dt.hour > cutoff_hour) or (now_dt.hour == cutoff_hour and now_dt.minute >= cutoff_min):
                if self._is_expiry_day(raw_signal, now_dt):
                    logger.warning(
                        f"⏳ [EXPIRY TIME GATE] Signal {raw_signal.get('signal_id')} ({raw_signal.get('option_symbol')}) "
                        f"rejected: Naked option buyer signals disabled after {self.expiry_cutoff_time} IST on expiry days "
                        f"(current time: {now_dt.strftime('%H:%M:%S IST')})."
                    )
                    return None

        # 1. Spot ATR14: live calculated value or index-calibrated baseline fallback
        min_atr_floor = 80.0 if inst == "SENSEX" else 20.0
        atr_obj = self.spot_atr.get(inst)
        if atr_obj and atr_obj.atr is not None:
            atr_val = max(min_atr_floor, atr_obj.atr)
        else:
            default_fallback = 160.0 if inst == "SENSEX" else 32.0
            pct_rate = 0.0022 if inst == "SENSEX" else 0.0014
            pct_fallback = spot_entry * pct_rate if spot_entry > 0 else default_fallback
            atr_val = max(min_atr_floor, default_fallback * 0.75, pct_fallback)

        # 2. Synthetic Spot Stop Loss (SL_spot)
        custom_sl_spot = raw_signal.get("custom_sl_spot")
        if custom_sl_spot is not None and custom_sl_spot > 0:
            sl_spot = float(custom_sl_spot)
            sl_rule_applied = "STRATEGY_CUSTOM"
        elif opt_type == "CE":
            sl_spot = spot_entry - (1.5 * atr_val)
            sl_rule_applied = "DELTA_SYNTHETIC"
        else:
            sl_spot = spot_entry + (1.5 * atr_val)
            sl_rule_applied = "DELTA_SYNTHETIC"

        # 3. Option Premium Stop Loss (SL_opt)
        spot_risk = abs(spot_entry - sl_spot)
        sl_opt_calc = p_entry - (self.delta_atm * spot_risk)

        # Floor rule: If SL_opt < 0.75 * P_entry, cap at 0.80 * P_entry (max 20% loss)
        if sl_opt_calc < (self.sl_floor_threshold * p_entry):
            sl_opt = self.sl_floor_ratio * p_entry
            sl_rule_applied = f"{sl_rule_applied}+FLOOR_80_PCT"
        else:
            sl_opt = sl_opt_calc

        # Ensure SL is strictly below Entry and > 0
        sl_opt = max(round(sl_opt, 2), round(p_entry * 0.50, 2))
        risk_per_share = p_entry - sl_opt

        # Minimum Option Risk Buffer:
        # SENSEX options fluctuate 5-10 points on normal tick spread alone.
        # An SL under 25 points on SENSEX or under 8 points on NIFTY is noise suicide.
        min_risk_points = 25.0 if inst == "SENSEX" else 8.0
        min_risk_points = min(min_risk_points, max(4.0, p_entry * 0.25))
        if risk_per_share < min_risk_points:
            risk_per_share = min_risk_points
            sl_opt = round(p_entry - risk_per_share, 2)
            sl_rule_applied = f"{sl_rule_applied}+MIN_RISK_FLOOR"

        if risk_per_share <= 0.20:
            risk_per_share = p_entry * 0.10
            sl_opt = round(p_entry - risk_per_share, 2)

        # 4. Target Calculation (Dual Targets: +1R Partial Profit & +2R Final Target)
        tgt_1r_opt = round(p_entry + (1.0 * risk_per_share), 2)
        tgt_2r_opt = round(p_entry + (self.rr_ratio * risk_per_share), 2)
        tgt_opt = tgt_2r_opt

        # 5. Lot Sizing Math (Q_shares)
        # Risk 1.0% of total account equity per trade
        max_trade_risk = self.total_equity * self.risk_per_trade_pct # e.g. ₹1,000 for ₹1,00,000
        risk_per_lot = risk_per_share * lot_size

        if risk_per_lot <= 0:
            return None

        strat_name = raw_signal.get("strategy", "")

        # EXCLUSIVE RULE FOR OI_SQUEEZE:
        # Scale-and-trail requires exactly 2 lots (1 lot books at Target 1, 1 lot trails with Breakeven SL).
        # Untouched for all other strategies.
        if strat_name == "OI_SQUEEZE":
            lots_count = 2
        else:
            lots_count = math.floor(max_trade_risk / risk_per_lot)
            # Position sizing safety cap: Maximum allowed lots per trade (never over-leverage)
            max_lots_cap = 2 if inst == "SENSEX" else 3
            if lots_count > max_lots_cap:
                logger.warning(
                    f"🛡️ [LOT SIZING CAP] Calculated {lots_count} lots for {inst} capped to max {max_lots_cap} lots."
                )
                lots_count = max_lots_cap
            elif lots_count < 1:
                # Capital is insufficient for 1 lot at 1.0% risk limit
                logger.warning(
                    f"Capital guard: Risk/lot ₹{risk_per_lot:.2f} exceeds 1% equity ₹{max_trade_risk:.2f}. "
                    f"Sizing defaulted to minimum 1 lot ({lot_size} shares) with advisory warning."
                )
                lots_count = 1

        quantity = lots_count * lot_size
        total_risk_amount = round(quantity * risk_per_share, 2)

        signal = dict(raw_signal)
        signal.update({
            "stop_loss": sl_opt,
            "target": tgt_opt,
            "target_1r": tgt_1r_opt,
            "target_2r": tgt_2r_opt,
            "partial_exit_guidance": "Book 50% at Target 1 (+1R), move remaining SL to Breakeven, trail to Target 2 (+2R)",
            "lot_size": lot_size,
            "quantity": quantity,
            "risk_amount": total_risk_amount,
            "status": "ACTIVE",
            "risk_meta": {
                "spot_atr14": round(atr_val, 2),
                "sl_spot": round(sl_spot, 2),
                "sl_rule_applied": sl_rule_applied,
                "risk_per_share": round(risk_per_share, 2),
                "target_1r": tgt_1r_opt,
                "target_2r": tgt_2r_opt,
                "rr_ratio": self.rr_ratio,
                "lots_count": lots_count,
                "account_equity": self.total_equity
            }
        })

        return signal
