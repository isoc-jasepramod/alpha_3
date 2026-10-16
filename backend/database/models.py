from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Date, JSON, Text
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class Signal(Base):
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_id = Column(String(64), unique=True, index=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    
    instrument = Column(String(32), nullable=False, index=True) # NIFTY, SENSEX
    strategy = Column(String(64), nullable=False, index=True)   # OI_SQUEEZE, ORB_BREAKOUT, VWAP_EMA, GAMMA_SCALP
    direction = Column(String(8), nullable=False)               # CE, PE
    
    strike = Column(Float, nullable=False)
    option_type = Column(String(8), nullable=False)
    option_token = Column(String(32), nullable=False, index=True)
    option_symbol = Column(String(64), nullable=False)
    
    spot_entry = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=False)
    target = Column(Float, nullable=False)
    
    lot_size = Column(Integer, nullable=False)
    quantity = Column(Integer, nullable=False)
    risk_amount = Column(Float, nullable=False)
    
    # State tracking: ACTIVE, INVALID_CHASE_PREVENTED, TARGET_HIT, STOP_HIT, EXPIRED
    status = Column(String(32), default="ACTIVE", index=True, nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    exit_price = Column(Float, nullable=True)
    theoretical_pnl = Column(Float, nullable=True)
    
    details = Column(JSON, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "signal_id": self.signal_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "instrument": self.instrument,
            "strategy": self.strategy,
            "direction": self.direction,
            "strike": self.strike,
            "option_type": self.option_type,
            "option_token": self.option_token,
            "option_symbol": self.option_symbol,
            "spot_entry": self.spot_entry,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "target": self.target,
            "lot_size": self.lot_size,
            "quantity": self.quantity,
            "risk_amount": self.risk_amount,
            "status": self.status,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolved_ts": self.resolved_at.timestamp() if self.resolved_at else None,
            "exit_price": self.exit_price,
            "theoretical_pnl": self.theoretical_pnl,
            "details": self.details or {}
        }


class DailyJournal(Base):
    __tablename__ = "daily_journal"

    date = Column(Date, primary_key=True, index=True)
    starting_equity = Column(Float, nullable=False, default=100000.0)
    current_equity = Column(Float, nullable=False, default=100000.0)
    realized_pnl = Column(Float, nullable=False, default=0.0)
    
    signals_count = Column(Integer, default=0)
    winning_trades = Column(Integer, default=0)
    losing_trades = Column(Integer, default=0)
    chase_prevented_count = Column(Integer, default=0)
    
    circuit_breaker_triggered = Column(Boolean, default=False)
    circuit_breaker_time = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            "date": str(self.date),
            "starting_equity": self.starting_equity,
            "current_equity": self.current_equity,
            "realized_pnl": self.realized_pnl,
            "signals_count": self.signals_count,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "chase_prevented_count": self.chase_prevented_count,
            "circuit_breaker_triggered": self.circuit_breaker_triggered,
            "circuit_breaker_time": self.circuit_breaker_time.isoformat() if self.circuit_breaker_time else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String(64), unique=True, index=True, nullable=False)
    parent_order_id = Column(String(64), index=True, nullable=True)
    signal_id = Column(String(64), index=True, nullable=True)

    instrument = Column(String(32), nullable=False, index=True) # NIFTY, SENSEX
    symbol = Column(String(64), nullable=False)                 # e.g., NIFTY24OCT25000CE
    option_symbol = Column(String(64), nullable=True)
    option_token = Column(String(32), nullable=True)
    direction = Column(String(16), nullable=True)               # CE, PE
    action = Column(String(32), default="BUY", nullable=False)  # BUY, SELL, FULL_EXIT, PARTIAL_EXIT
    order_type = Column(String(32), default="LIMIT", nullable=False) # LIMIT, MARKET
    product_type = Column(String(32), default="INTRADAY", nullable=False) # INTRADAY
    mode = Column(String(32), default="PAPER", nullable=False)  # PAPER, LIVE_EXCHANGE


    quantity = Column(Integer, nullable=False)
    filled_quantity = Column(Integer, default=0)
    price = Column(Float, nullable=True)                        # Planned / limit price
    fill_price = Column(Float, nullable=True)                   # Real executed price
    slippage = Column(Float, default=0.0)
    exit_price = Column(Float, nullable=True)
    realized_pnl = Column(Float, default=0.0)
    total_realized_pnl = Column(Float, default=0.0)

    # State tracking: PENDING, FILLED, REJECTED, ABORTED_UNFILLED, PARTIAL_CLOSED, CLOSED, EXIT_FAILED
    status = Column(String(32), default="PENDING", index=True, nullable=False)
    reason = Column(Text, nullable=True)                        # Rejection or exit reason

    placed_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    filled_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    duration_seconds = Column(Integer, nullable=True)

    broker_order_id = Column(String(64), nullable=True)
    broker_payload = Column(JSON, nullable=True)
    metadata_json = Column(JSON, nullable=True)

    def to_dict(self):
        dur_str = None
        if self.duration_seconds is not None:
            dur_sec = self.duration_seconds
            if dur_sec < 60:
                dur_str = f"{dur_sec}s"
            elif dur_sec < 3600:
                m, s = divmod(dur_sec, 60)
                dur_str = f"{m}m {s}s" if s > 0 else f"{m}m"
            else:
                h, rem = divmod(dur_sec, 3600)
                m = rem // 60
                dur_str = f"{h}h {m}m"

        entry_t = self.filled_at.isoformat() if self.filled_at else (self.placed_at.isoformat() if self.placed_at else None)
        exit_t = self.closed_at.isoformat() if self.closed_at else None

        return {
            "id": self.id,
            "order_id": self.order_id,
            "parent_order_id": self.parent_order_id,
            "signal_id": self.signal_id,
            "instrument": self.instrument,
            "symbol": self.symbol,
            "option_symbol": self.option_symbol,
            "option_token": self.option_token,
            "direction": self.direction,
            "action": self.action,
            "order_type": self.order_type,
            "product_type": self.product_type,
            "mode": self.mode,
            "quantity": self.quantity,
            "filled_quantity": self.filled_quantity,
            "price": self.price,
            "fill_price": self.fill_price,
            "slippage": self.slippage,
            "exit_price": self.exit_price,
            "realized_pnl": self.realized_pnl,
            "total_realized_pnl": self.total_realized_pnl,
            "status": self.status,
            "reason": self.reason,
            "placed_at": self.placed_at.isoformat() if self.placed_at else None,
            "filled_at": self.filled_at.isoformat() if self.filled_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "entry_time": entry_t,
            "exit_time": exit_t,
            "timestamp": exit_t or entry_t,
            "duration_seconds": self.duration_seconds,
            "duration_str": dur_str,
            "broker_order_id": self.broker_order_id,
            "broker_payload": self.broker_payload or {},
            "metadata": self.metadata_json or {}
        }


