import asyncio
import json
from datetime import datetime, timezone, date
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query, BackgroundTasks
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from backend.database.db import get_db, AsyncSessionLocal
from backend.database.models import Signal, DailyJournal
from backend.core.redis_bus import RedisBus
from lab_services.replay_engine import MarketReplayEngine

router = APIRouter()

# Global state reference injected by server
class AppState:
    redis_bus: Optional[RedisBus] = None
    instrument_manager: Any = None
    risk_governor: Any = None
    signal_tracker: Any = None
    replay_engine: Optional[MarketReplayEngine] = None
    connected_websockets: List[WebSocket] = []
    latest_ticks_buffer: Dict[str, Dict[str, Any]] = {} # token -> tick
    circuit_breaker_status: bool = False
    spot_data: Dict[str, Dict[str, Any]] = {}

app_state = AppState()

@router.get("/api/telemetry")
async def get_telemetry():
    active_sigs = len(app_state.signal_tracker.active_signals) if app_state.signal_tracker else 0
    realized_pnl = app_state.risk_governor.realized_daily_pnl if app_state.risk_governor else 0.0
    equity = app_state.risk_governor.total_equity if app_state.risk_governor else 100000.0
    cb_tripped = app_state.risk_governor.circuit_breaker_tripped if app_state.risk_governor else False

    atm_data = {k: v.get("atm") for k, v in app_state.spot_data.items() if v.get("atm")}
    if not atm_data and app_state.instrument_manager:
        atm_data = getattr(app_state.instrument_manager, "current_atm", {})

    return {
        "status": "ONLINE",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_equity": equity,
        "realized_pnl": round(realized_pnl, 2),
        "realized_pnl_pct": round((realized_pnl / equity) * 100.0, 2),
        "circuit_breaker_tripped": cb_tripped,
        "active_signals_count": active_sigs,
        "connected_clients": len(app_state.connected_websockets),
        "current_atm": atm_data,
        "spot_data": app_state.spot_data
    }

@router.get("/api/signals/active")
async def get_active_signals():
    if not app_state.signal_tracker:
        return []
    return list(app_state.signal_tracker.active_signals.values())

@router.get("/api/signals/history")
async def get_signal_history(limit: int = 50, session: AsyncSession = Depends(get_db)):
    stmt = select(Signal).order_by(desc(Signal.created_at)).limit(limit)
    res = await session.execute(stmt)
    records = res.scalars().all()
    return [r.to_dict() for r in records]

@router.get("/api/journal/today")
async def get_daily_journal(session: AsyncSession = Depends(get_db)):
    today_date = date.today()
    stmt = select(DailyJournal).where(DailyJournal.date == today_date)
    res = await session.execute(stmt)
    journal = res.scalar_one_or_none()
    if not journal:
        return {
            "date": str(today_date),
            "starting_equity": 100000.0,
            "current_equity": 100000.0,
            "realized_pnl": 0.0,
            "signals_count": 0,
            "circuit_breaker_triggered": False
        }
    return journal.to_dict()

@router.post("/api/lab/replay/start")
async def start_replay(background_tasks: BackgroundTasks, speed: float = Query(2.0, ge=1.0, le=10.0)):
    if not app_state.replay_engine:
        app_state.replay_engine = MarketReplayEngine(
            speed_multiplier=speed,
            target_channel=app_state.redis_bus.market_channel
        )
    
    background_tasks.add_task(app_state.replay_engine.replay, speed=speed, target_channel=app_state.redis_bus.market_channel)
    return {"status": "REPLAY_STARTED", "speed": speed}

@router.post("/api/lab/replay/stop")
async def stop_replay():
    if app_state.replay_engine:
        app_state.replay_engine.stop()
        return {"status": "REPLAY_STOPPED"}
    return {"status": "NO_REPLAY_ACTIVE"}

@router.websocket("/ws/stream")
async def websocket_stream(websocket: WebSocket):
    await websocket.accept()
    app_state.connected_websockets.append(websocket)
    logger.info(f"New client connected to /ws/stream. Total clients: {len(app_state.connected_websockets)}")

    # Send initial state dump
    try:
        init_payload = {
            "type": "INITIAL_STATE",
            "active_signals": list(app_state.signal_tracker.active_signals.values()) if app_state.signal_tracker else [],
            "telemetry": await get_telemetry()
        }
        await websocket.send_text(json.dumps(init_payload))

        # Keep client connection open to read any client ping/pong
        while True:
            msg = await websocket.receive_text()
            # Client ping/pong
            if msg == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        logger.info("Client disconnected from /ws/stream.")
    except Exception as e:
        logger.warning(f"WebSocket client exception: {e}")
    finally:
        if websocket in app_state.connected_websockets:
            app_state.connected_websockets.remove(websocket)
