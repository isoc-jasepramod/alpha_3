import asyncio
import json
from datetime import datetime, timezone, date, timedelta
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

def get_today_ist_boundaries():
    """Returns (today_ist_date, start_utc, end_utc) for the Indian Standard Time (IST) trading day."""
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist)
    today_ist = now_ist.date()
    start_ist = datetime(today_ist.year, today_ist.month, today_ist.day, 0, 0, 0, tzinfo=ist)
    start_utc = start_ist.astimezone(timezone.utc)
    end_utc = (start_ist + timedelta(days=1)).astimezone(timezone.utc)
    return today_ist, start_utc, end_utc

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

    # Calculate accurate Advisory Theoretical PnL for TODAY in IST
    theoretical_pnl = 0.0
    today_signals_count = 0
    today_target_hits = 0
    today_stop_hits = 0
    today_chase_prevented = 0

    if app_state.signal_tracker and hasattr(app_state.signal_tracker, "today_theoretical_pnl"):
        theoretical_pnl = float(app_state.signal_tracker.today_theoretical_pnl)
        today_signals_count = int(getattr(app_state.signal_tracker, "today_signals_count", 0))
        today_target_hits = int(getattr(app_state.signal_tracker, "today_target_hits", 0))
        today_stop_hits = int(getattr(app_state.signal_tracker, "today_stop_hits", 0))
        today_chase_prevented = int(getattr(app_state.signal_tracker, "today_chase_prevented", 0))
    elif app_state.risk_governor:
        theoretical_pnl = float(app_state.risk_governor.realized_daily_pnl)

    # Broker Execution PnL (from execution bridge / audit logs)
    broker_pnl = 0.0
    if app_state.execution_bridge and hasattr(app_state.execution_bridge, "risk_governor") and app_state.execution_bridge.risk_governor:
        broker_pnl = float(getattr(app_state.execution_bridge.risk_governor, "realized_daily_pnl", 0.0))
    else:
        try:
            import os
            today_file = f"data/execution_logs/orders_{datetime.now().strftime('%Y-%m-%d')}.json"
            if os.path.exists(today_file):
                with open(today_file, "r", encoding="utf-8") as f:
                    o_log = json.load(f)
                broker_pnl = sum(float(o.get("total_realized_pnl", 0.0)) for o in o_log if o.get("action") == "FULL_EXIT")
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
        "theoretical_pnl": round(theoretical_pnl, 2),
        "theoretical_pnl_pct": round((theoretical_pnl / equity) * 100.0, 2),
        "realized_pnl": round(theoretical_pnl, 2), # Maintained for legacy compatibility with frontend stat-value
        "realized_pnl_pct": round((theoretical_pnl / equity) * 100.0, 2),
        "broker_realized_pnl": round(broker_pnl, 2),
        "today_signals_count": today_signals_count,
        "today_target_hits": today_target_hits,
        "today_stop_hits": today_stop_hits,
        "today_chase_prevented": today_chase_prevented,
        "circuit_breaker_tripped": cb_tripped,
        "active_signals_count": active_sigs,
        "connected_clients": len(app_state.connected_websockets),
        "current_atm": atm_data,
        "spot_data": app_state.spot_data,
        "gamma": gamma_data,
        "regimes": regimes
    }

@router.get("/api/signals/today-summary")
async def get_today_signal_summary(session: AsyncSession = Depends(get_db)):
    """Returns aggregated theoretical metrics for all signals generated today in IST."""
    today_ist, start_utc, end_utc = get_today_ist_boundaries()
    stmt = select(Signal).where(Signal.created_at >= start_utc, Signal.created_at < end_utc)
    res = await session.execute(stmt)
    records = res.scalars().all()

    t_hits = sum(1 for r in records if r.status == "TARGET_HIT")
    s_hits = sum(1 for r in records if r.status == "STOP_HIT")
    c_prev = sum(1 for r in records if r.status == "INVALID_CHASE_PREVENTED")
    active = sum(1 for r in records if r.status == "ACTIVE")
    pnl = sum(float(r.theoretical_pnl or 0.0) for r in records)
    decided = t_hits + s_hits
    win_rate = round((t_hits / decided * 100.0), 1) if decided > 0 else 0.0

    return {
        "date": str(today_ist),
        "total_signals": len(records),
        "target_hits": t_hits,
        "stop_hits": s_hits,
        "chase_prevented": c_prev,
        "active_signals": active,
        "theoretical_pnl": round(pnl, 2),
        "win_rate": win_rate
    }

@router.get("/api/signals/active")
async def get_active_signals():
    if not app_state.signal_tracker:
        return []
    return list(app_state.signal_tracker.active_signals.values())

@router.get("/api/signals/history")
async def get_signal_history(limit: int = 50, date_filter: Optional[str] = None, session: AsyncSession = Depends(get_db)):
    if date_filter == "today":
        _, start_utc, end_utc = get_today_ist_boundaries()
        stmt = select(Signal).where(Signal.created_at >= start_utc, Signal.created_at < end_utc).order_by(desc(Signal.created_at)).limit(limit)
    else:
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
async def get_execution_orders(date_str: Optional[str] = None):
    """Returns the list of orders executed (simulated in Phase 1 Paper mode), optionally filtered by date."""
    bridge = getattr(app_state, "execution_bridge", None)
    today_str = datetime.now().strftime("%Y-%m-%d")

    # If a historical date is requested, load directly from the date-partitioned JSON log
    if date_str and date_str != today_str:
        import os, json
        file_path = f"data/execution_logs/orders_{date_str}.json"
        if not os.path.exists(file_path):
            return []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                raw_orders = json.load(f)
            orders_map = {}
            for r in raw_orders:
                oid = r.get("order_id")
                if oid and not oid.startswith("DRY-EXIT-") and not oid.startswith("DRY-T1-"):
                    orders_map[oid] = r
                elif oid and (oid.startswith("DRY-EXIT-") or oid.startswith("DRY-T1-")):
                    p_oid = r.get("parent_order_id")
                    if p_oid and p_oid in orders_map:
                        orders_map[p_oid]["status"] = "CLOSED"
                        orders_map[p_oid]["exit_price"] = r.get("exit_price")
                        orders_map[p_oid]["exit_reason"] = r.get("reason", "TARGET_HIT")
                        orders_map[p_oid]["total_realized_pnl"] = r.get("total_realized_pnl", 0.0)
                        orders_map[p_oid]["closed_at"] = r.get("timestamp")

            flattened = []
            for ord in orders_map.values():
                o = dict(ord)
                meta = o.get("metadata") or {}
                broker = o.get("broker_payload") or {}
                o["timestamp"] = o.get("closed_at") or o.get("filled_at") or o.get("placed_at")
                o["symbol"] = o.get("symbol") or meta.get("option_symbol") or broker.get("tradingsymbol") or "-"
                o["direction"] = o.get("direction") or meta.get("direction") or ("PE" if str(o["symbol"]).endswith("PE") else "CE")
                o["action"] = o.get("action") or broker.get("transactiontype") or "BUY"
                pnl = o.get("total_realized_pnl")
                if pnl is None:
                    pnl = o.get("realized_pnl", 0.0)
                o["realized_pnl"] = round(float(pnl), 2)
                flattened.append(o)
            return flattened
        except Exception as e:
            logger.warning(f"Error loading historical orders for {date_str}: {e}")
            return []

    if not bridge:
        return []
    flattened = []
    for ord in bridge.placed_orders.values():
        o = dict(ord)
        meta = o.get("metadata") or {}
        broker = o.get("broker_payload") or {}
        o["timestamp"] = o.get("closed_at") or o.get("filled_at") or o.get("placed_at")
        o["symbol"] = o.get("symbol") or meta.get("option_symbol") or broker.get("tradingsymbol") or "-"
        o["direction"] = o.get("direction") or meta.get("direction") or ("PE" if str(o["symbol"]).endswith("PE") else "CE")
        o["action"] = o.get("action") or broker.get("transactiontype") or "BUY"
        pnl = o.get("total_realized_pnl")
        if pnl is None:
            pnl = o.get("realized_pnl", 0.0)
        o["realized_pnl"] = round(float(pnl), 2)
        flattened.append(o)
    return flattened

@router.get("/api/execution/daily-summary")
async def get_execution_daily_summary():
    """Returns day-wise execution PnL, win rates, and trade statistics across all session days."""
    import glob, os, json
    files = sorted(glob.glob("data/execution_logs/orders_*.json"), reverse=True)
    summaries = []

    for fpath in files:
        fname = os.path.basename(fpath)
        d_str = fname.replace("orders_", "").replace(".json", "")
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                records = json.load(f)
            orders = [r for r in records if not r.get("order_id", "").startswith("DRY-EXIT-") and not r.get("order_id", "").startswith("DRY-T1-")]
            exits = [r for r in records if r.get("order_id", "").startswith("DRY-EXIT-") or r.get("order_id", "").startswith("DRY-T1-")]
            wins = [e for e in exits if float(e.get("total_realized_pnl", 0.0)) > 0]
            losses = [e for e in exits if float(e.get("total_realized_pnl", 0.0)) < 0]
            be = [e for e in exits if float(e.get("total_realized_pnl", 0.0)) == 0]
            pnl = sum(float(e.get("total_realized_pnl", 0.0)) for e in exits)
            gross_win = sum(float(e.get("total_realized_pnl", 0.0)) for e in wins)
            gross_loss = sum(float(e.get("total_realized_pnl", 0.0)) for e in losses)
            decided = len(wins) + len(losses)
            win_rate = round((len(wins) / decided * 100.0), 1) if decided > 0 else 0.0
            profit_factor = round(abs(gross_win / gross_loss), 2) if gross_loss != 0 else 0.0

            summaries.append({
                "date": d_str,
                "orders_count": len(orders),
                "trades_count": len(exits),
                "wins": len(wins),
                "losses": len(losses),
                "breakeven": len(be),
                "win_rate": win_rate,
                "gross_profit": round(gross_win, 2),
                "gross_loss": round(gross_loss, 2),
                "profit_factor": profit_factor,
                "realized_pnl": round(pnl, 2),
                "roi_pct": round((pnl / 25000.0) * 100.0, 2)
            })
        except Exception as e:
            logger.warning(f"Error computing daily execution summary for {fname}: {e}")

    return summaries

@router.get("/api/execution/status")
async def get_execution_status():
    """Returns the status, safety gates, and limits of the Execution Bridge."""
    bridge = getattr(app_state, "execution_bridge", None)
    if not bridge:
        return {"enabled": False, "mode": "DISABLED"}
    cap_metrics = bridge.get_capital_metrics() if hasattr(bridge, "get_capital_metrics") else {}
    realized_pnl = float(getattr(bridge.risk_governor, "realized_daily_pnl", 0.0)) if hasattr(bridge, "risk_governor") and bridge.risk_governor else 0.0
    return {
        "enabled": bridge.enabled,
        "mode": bridge.mode,
        "dry_run": bridge.dry_run,
        "kill_switch_active": bridge.kill_switch_active,
        "active_positions_count": len(bridge.active_positions),
        "total_orders_placed": len(bridge.placed_orders),
        "session_realized_pnl": round(realized_pnl, 2),
        "session_roi_pct": round((realized_pnl / 25000.0) * 100.0, 2),
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
    """Returns the active open positions in the execution bridge with flattened metadata and live mark-to-market prices."""
    bridge = getattr(app_state, "execution_bridge", None)
    if not bridge:
        return []

    tracker = getattr(app_state, "signal_tracker", None)
    active_sigs = tracker.active_signals if tracker else {}
    token_to_sig = {str(s.get("option_token")): s for s in active_sigs.values() if s.get("option_token")}

    flattened = []
    for pos in bridge.active_positions.values():
        p = dict(pos)
        meta = p.get("metadata") or {}
        broker = p.get("broker_payload") or {}
        sig_id = p.get("signal_id")
        opt_token = str(p.get("option_token") or meta.get("option_token") or broker.get("symboltoken") or "")

        # Resolve live LTP from SignalTracker or latest buffer
        live_sig = active_sigs.get(sig_id) or token_to_sig.get(opt_token) or {}
        live_ltp = live_sig.get("live_ltp")
        if (live_ltp is None or live_ltp <= 0) and hasattr(app_state, "latest_ticks_buffer"):
            tick_obj = app_state.latest_ticks_buffer.get(opt_token, {})
            if isinstance(tick_obj, dict):
                live_ltp = tick_obj.get("ltp")

        fill_price = float(p.get("fill_price") or meta.get("expected_entry") or 0.0)
        current_ltp = float(live_ltp) if (live_ltp and live_ltp > 0) else fill_price
        rem_qty = int(p.get("remaining_quantity") or p.get("quantity") or meta.get("quantity") or 0)
        unrealized = round((current_ltp - fill_price) * rem_qty, 2)

        p["current_ltp"] = round(current_ltp, 2)
        p["unrealized_pnl"] = unrealized

        # Keep bridge pos synced in memory
        pos["current_ltp"] = round(current_ltp, 2)
        pos["unrealized_pnl"] = unrealized

        for key in ["instrument", "strike", "direction", "strategy", "option_symbol", "stop_loss", "target", "target_1r", "target_2r", "lots_count", "exit_mode", "exit_guidance"]:
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
    if hasattr(bridge, "save_config"):
        bridge.save_config()
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
