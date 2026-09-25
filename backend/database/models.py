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
