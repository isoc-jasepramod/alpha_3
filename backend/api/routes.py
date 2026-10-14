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
    regime_filter: Any = None
    execution_bridge: Any = None

app_state = AppState()

@router.get("/api/telemetry")
async def get_telemetry():
    active_sigs = len(app_state.signal_tracker.active_signals) if app_state.signal_tracker else 0
    realized_pnl = float(app_state.risk_governor.realized_daily_pnl) if app_state.risk_governor else 0.0
    
    # Reconcile with all-day persistent execution log (accounting for any mid-day restarts)
    try:
        import os, json
        today_file = f"data/execution_logs/orders_{datetime.now().strftime('%Y-%m-%d')}.json"
        if os.path.exists(today_file):
            with open(today_file, "r", encoding="utf-8") as f:
                o_log = json.load(f)
            file_tot = sum(float(o.get("total_realized_pnl", 0.0)) for o in o_log if o.get("action") == "FULL_EXIT")
            if file_tot != 0.0:
                realized_pnl = file_tot
    except Exception:
        pass

    equity = app_state.risk_governor.total_equity if app_state.risk_governor else 100000.0
    cb_tripped = app_state.risk_governor.circuit_breaker_tripped if app_state.risk_governor else False

    atm_data = {k: v.get("atm") for k, v in app_state.spot_data.items() if v.get("atm")}
    if not atm_data and app_state.instrument_manager:
        atm_data = getattr(app_state.instrument_manager, "current_atm", {})

    regimes = {}
    if app_state.regime_filter:
        try:
            regimes = {
                "NIFTY": app_state.regime_filter.get_regime("NIFTY"),
                "SENSEX": app_state.regime_filter.get_regime("SENSEX")
            }
        except Exception:
            pass

    gamma_data = {}
    if hasattr(app_state, "gex_engine") and app_state.gex_engine:
        gamma_data = getattr(app_state.gex_engine, "latest_gex", {})

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
        "spot_data": app_state.spot_data,
        "gamma": gamma_data,
        "regimes": regimes
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

@router.post("/api/reports/send_periodic_digest")
async def trigger_periodic_digest(milestone: Optional[str] = Query(None)):
    """Triggers an immediate 45-min periodic analysis digest to Telegram."""
    reporter = getattr(app_state, "periodic_reporter", None)
    if reporter:
        success = await reporter.send_digest(milestone_str=milestone or "Manual Trigger")
        return {"status": "SUCCESS" if success else "FAILED", "sent": success}
    return {"status": "ERROR", "message": "Periodic reporter not active on engine"}

@router.get("/api/reports/digest_preview")
async def get_digest_preview():
    """Returns the current 45-min digest payload and formatted HTML preview without sending."""
    reporter = getattr(app_state, "periodic_reporter", None)
    if reporter:
        payload = reporter.build_digest_payload()
        html = reporter.format_digest_html(payload, milestone_str="Preview")
        return {"status": "SUCCESS", "payload": payload, "html": html}
    return {"status": "ERROR", "message": "Periodic reporter not active on engine"}

@router.get("/api/execution/orders")
async def get_execution_orders():
    """Returns the list of orders executed (simulated in Phase 1 Paper mode)."""
    bridge = getattr(app_state, "execution_bridge", None)
    if not bridge:
        return []
    return bridge.get_audit_history()

@router.get("/api/execution/status")
async def get_execution_status():
    """Returns the status, safety gates, and limits of the Execution Bridge."""
    bridge = getattr(app_state, "execution_bridge", None)
    if not bridge:
        return {"enabled": False, "mode": "DISABLED"}
    cap_metrics = bridge.get_capital_metrics() if hasattr(bridge, "get_capital_metrics") else {}
    return {
        "enabled": bridge.enabled,
        "mode": bridge.mode,
        "dry_run": bridge.dry_run,
        "kill_switch_active": bridge.kill_switch_active,
        "active_positions_count": len(bridge.active_positions),
        "total_orders_placed": len(bridge.placed_orders),
        "max_capital_per_trade": bridge.max_capital_per_trade,
        "max_daily_loss": bridge.max_daily_loss,
        "allowed_strategies": getattr(bridge, "allowed_strategies", []),
        "nifty_lots": getattr(bridge, "nifty_lots", 1),
        "sensex_lots": getattr(bridge, "sensex_lots", 1),
        "force_single_lot": getattr(bridge.risk_governor, "force_single_lot", True) if hasattr(bridge, "risk_governor") else True,
        "capital_metrics": cap_metrics
    }

@router.get("/api/execution/positions")
async def get_execution_positions():
    """Returns the active open positions in the execution bridge with flattened metadata."""
    bridge = getattr(app_state, "execution_bridge", None)
    if not bridge:
        return []
    flattened = []
    for pos in bridge.active_positions.values():
        p = dict(pos)
        meta = p.get("metadata") or {}
        for key in ["instrument", "strike", "direction", "strategy", "option_symbol", "stop_loss", "target", "target_1r", "target_2r"]:
            if key not in p or p[key] is None:
                if key in meta:
                    p[key] = meta[key]
        flattened.append(p)
    return flattened

@router.post("/api/execution/exit-position")
async def exit_single_position(signal_id: str = Query(...)):
    """Manually exits/squares off a specific active position."""
    bridge = getattr(app_state, "execution_bridge", None)
    if not bridge:
        return {"status": "ERROR", "message": "Execution bridge not active on engine"}
    pos = bridge.active_positions.get(signal_id)
    if not pos:
        return {"status": "NOT_FOUND", "message": f"No active position found for signal {signal_id}"}
    exit_price = pos.get("current_ltp", pos.get("fill_price", 0.0))
    res = await bridge.handle_full_exit({"signal_id": signal_id}, exit_price=exit_price, reason="MANUAL_UI_EXIT")
    return {"status": "SUCCESS", "result": res}

@router.post("/api/execution/toggle-arm")
async def toggle_execution_arm(armed: bool = Query(...)):
    """Arms or disarms live trade routing to broker."""
    bridge = getattr(app_state, "execution_bridge", None)
    if not bridge:
        return {"status": "ERROR", "message": "Execution bridge not active on engine"}
    bridge.enabled = armed
    return {"status": "SUCCESS", "armed": bridge.enabled}

@router.get("/api/execution/config")
async def get_execution_config():
    """Returns execution configuration options."""
    bridge = getattr(app_state, "execution_bridge", None)
    if not bridge:
        return {}
    cap_metrics = bridge.get_capital_metrics() if hasattr(bridge, "get_capital_metrics") else {}
    return {
        "enabled": bridge.enabled,
        "allowed_strategies": getattr(bridge, "allowed_strategies", []),
        "max_capital_per_trade": bridge.max_capital_per_trade,
        "max_daily_loss": bridge.max_daily_loss,
        "nifty_lots": getattr(bridge, "nifty_lots", 1),
        "sensex_lots": getattr(bridge, "sensex_lots", 1),
        "total_capital": cap_metrics.get("total_capital", 25000.0),
        "force_single_lot": getattr(bridge.risk_governor, "force_single_lot", True) if hasattr(bridge, "risk_governor") else True,
        "capital_metrics": cap_metrics
    }

@router.post("/api/execution/config")
async def update_execution_config(payload: Dict[str, Any]):
    """Updates runtime execution configuration."""
    bridge = getattr(app_state, "execution_bridge", None)
    if not bridge:
        return {"status": "ERROR", "message": "Execution bridge not active"}
    if "allowed_strategies" in payload:
        bridge.allowed_strategies = list(payload["allowed_strategies"])
    if "max_capital_per_trade" in payload:
        bridge.max_capital_per_trade = float(payload["max_capital_per_trade"])
    if "total_capital" in payload and hasattr(bridge, "risk_governor"):
        bridge.risk_governor.total_capital = float(payload["total_capital"])
    if "force_single_lot" in payload and hasattr(bridge, "risk_governor"):
        bridge.risk_governor.force_single_lot = bool(payload["force_single_lot"])
    if "max_daily_loss" in payload:
        bridge.max_daily_loss = float(payload["max_daily_loss"])
        if hasattr(bridge, "risk_governor"):
            bridge.risk_governor.max_daily_loss = float(payload["max_daily_loss"])
    if "nifty_lots" in payload:
        bridge.nifty_lots = int(payload["nifty_lots"])
    if "sensex_lots" in payload:
        bridge.sensex_lots = int(payload["sensex_lots"])
    cap_metrics = bridge.get_capital_metrics() if hasattr(bridge, "get_capital_metrics") else {}
    return {
        "status": "SUCCESS",
        "config": {
            "allowed_strategies": bridge.allowed_strategies,
            "max_capital_per_trade": bridge.max_capital_per_trade,
            "total_capital": cap_metrics.get("total_capital", 25000.0),
            "force_single_lot": getattr(bridge.risk_governor, "force_single_lot", True) if hasattr(bridge, "risk_governor") else True,
            "max_daily_loss": bridge.max_daily_loss,
            "nifty_lots": bridge.nifty_lots,
            "sensex_lots": bridge.sensex_lots,
            "capital_metrics": cap_metrics
        }
    }

@router.post("/api/execution/kill-switch")
async def toggle_kill_switch(active: bool = Query(True)):
    """Toggles the emergency execution kill switch and squares off all open positions."""
    bridge = getattr(app_state, "execution_bridge", None)
    if not bridge:
        return {"status": "ERROR", "message": "Execution bridge not active on engine"}
    closed_positions = []
    if active:
        bridge.activate_kill_switch(reason="Manual Emergency UI Trigger")
        if hasattr(bridge, "square_off_all_positions"):
            closed_positions = await bridge.square_off_all_positions(reason="EMERGENCY_KILL_SWITCH")
    else:
        bridge.deactivate_kill_switch()
    return {
        "status": "SUCCESS",
        "kill_switch_active": bridge.kill_switch_active,
        "closed_positions": closed_positions
    }
