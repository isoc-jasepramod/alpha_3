import os
import yaml
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from backend.database.db import init_db, AsyncSessionLocal
from backend.database.models import Signal
from backend.core.redis_bus import RedisBus
from backend.core.instrument_manager import InstrumentManager
from backend.risk.risk_governor import RiskGovernor
from backend.risk.signal_tracker import SignalTracker
from backend.core.auth import AngelOneAuth
from backend.core.websocket_client import SmartAPIWebSocketClient
from backend.strategies.oi_squeeze import OISqueezeSentinel
from backend.strategies.orb_breakout import VolumeBackedORB
from backend.strategies.vwap_ema import VWAPEMAAlignment
from backend.strategies.gamma_scalp import ExpiryDayGammaScalp
from backend.strategies.momentum_impulse import MomentumImpulseDetector
from backend.strategies.iv_engine import IVEngine
from backend.strategies.gex_engine import GEXEngine
from backend.strategies.flow_engine import FlowEngine
from backend.strategies.squeeze_detector import SqueezeDetector
from backend.core.chain_poller import OptionChainPoller
from backend.core.telegram_notifier import TelegramNotifier
from backend.api.routes import router, app_state

class EngineCoordinator:
    def __init__(self):
        self.redis_bus = RedisBus.from_config()
        self.instrument_mgr = InstrumentManager()
        self.risk_governor = RiskGovernor()
        self.signal_tracker = SignalTracker(self.redis_bus, self.risk_governor)
        self.auth = AngelOneAuth()
        self.telegram = TelegramNotifier()
        self.ws_client: Optional[SmartAPIWebSocketClient] = None
        
        # Load strategy configuration
        rules_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "market_rules.yaml")
        strat_cfg = {}
        if os.path.exists(rules_path):
            try:
                with open(rules_path, "r") as f:
                    strat_cfg = yaml.safe_load(f).get("strategies", {})
            except Exception as e:
                logger.warning(f"Failed to load market_rules.yaml: {e}")

        # Core and Precursor Predictive Engines
        iv_engine = IVEngine(strat_cfg.get("iv_engine"))
        gex_engine = GEXEngine(iv_engine=iv_engine, config=strat_cfg.get("gex_engine"))
        flow_engine = FlowEngine(strat_cfg.get("flow_engine"))
        squeeze_detector = SqueezeDetector(strat_cfg.get("squeeze_detector"))

        self.chain_poller = OptionChainPoller(
            redis_bus=self.redis_bus,
            instrument_mgr=self.instrument_mgr,
            poll_interval_sec=60
        )

        # Strategy instances with configured rules
        self.strategies = [
            OISqueezeSentinel(strat_cfg.get("oi_squeeze")),
            VolumeBackedORB(strat_cfg.get("orb_breakout")),
            VWAPEMAAlignment(strat_cfg.get("vwap_ema")),
            ExpiryDayGammaScalp(strat_cfg.get("gamma_scalp")),
            MomentumImpulseDetector(strat_cfg.get("momentum_impulse")),
            iv_engine,
            gex_engine,
            flow_engine,
            squeeze_detector
        ]


        self.running = False
        self.tasks: List[asyncio.Task] = []
        self.token_meta_cache: Dict[str, Dict[str, Any]] = {}
        self.nifty_spot = 23346.4
        self.sensex_spot = 74294.96

        # Initialize baseline spot data
        app_state.spot_data = {
            "NIFTY": {
                "symbol": "NIFTY",
                "name": "NIFTY 50",
                "token": "99926000",
                "ltp": self.nifty_spot,
                "change": 0.0,
                "change_pct": 0.0,
                "open": self.nifty_spot,
                "high": self.nifty_spot,
                "low": self.nifty_spot,
                "close": self.nifty_spot,
                "atm": 23350.0
            },
            "SENSEX": {
                "symbol": "SENSEX",
                "name": "SENSEX",
                "token": "99919000",
                "ltp": self.sensex_spot,
                "change": 0.0,
                "change_pct": 0.0,
                "open": self.sensex_spot,
                "high": self.sensex_spot,
                "low": self.sensex_spot,
                "close": self.sensex_spot,
                "atm": 74300.0
            }
        }

    async def _fetch_spot_quotes(self):
        """Fetches latest real quotes for NIFTY and SENSEX from AngelOne SmartAPI or fallback"""
        if self.auth and self.auth.smart_connect:
            # Fetch NIFTY
            try:
                res = self.auth.smart_connect.ltpData("NSE", "Nifty 50", "99926000")
                if res and res.get("status") and res.get("data"):
                    d = res["data"]
                    ltp = float(d.get("ltp", 0.0))
                    close = float(d.get("close") or ltp)
                    if ltp > 0:
                        self.nifty_spot = ltp
                        chg = round(ltp - close, 2)
                        chg_pct = round((chg / close) * 100, 2) if close > 0 else 0.0
                        open_p = float(d.get("open") or ltp)
                        high_p = float(d.get("high") or ltp)
                        low_p = float(d.get("low") or ltp)
                        app_state.spot_data["NIFTY"] = {
                            "symbol": "NIFTY",
                            "name": "NIFTY 50",
                            "token": "99926000",
                            "ltp": ltp,
                            "change": chg,
                            "change_pct": chg_pct,
                            "open": open_p if open_p > 0 else ltp,
                            "high": high_p if high_p > 0 else ltp,
                            "low": low_p if low_p > 0 else ltp,
                            "close": close if close > 0 else ltp,
                            "atm": self.instrument_mgr.calculate_atm_strike("NIFTY", ltp)
                        }
                        logger.info(f"Retrieved live AngelOne NIFTY spot quote: {ltp} (ATM: {app_state.spot_data['NIFTY']['atm']})")
            except Exception as e:
                logger.warning(f"Failed to fetch AngelOne NIFTY quote: {e}")

            # Fetch SENSEX
            try:
                res = self.auth.smart_connect.ltpData("BSE", "SENSEX", "99919000")
                if res and res.get("status") and res.get("data"):
                    d = res["data"]
                    ltp = float(d.get("ltp", 0.0))
                    close = float(d.get("close") or ltp)
                    if ltp > 0:
                        self.sensex_spot = ltp
                        chg = round(ltp - close, 2)
                        chg_pct = round((chg / close) * 100, 2) if close > 0 else 0.0
                        open_p = float(d.get("open") or ltp)
                        high_p = float(d.get("high") or ltp)
                        low_p = float(d.get("low") or ltp)
                        app_state.spot_data["SENSEX"] = {
                            "symbol": "SENSEX",
                            "name": "SENSEX",
                            "token": "99919000",
                            "ltp": ltp,
                            "change": chg,
                            "change_pct": chg_pct,
                            "open": open_p if open_p > 0 else ltp,
                            "high": high_p if high_p > 0 else ltp,
                            "low": low_p if low_p > 0 else ltp,
                            "close": close if close > 0 else ltp,
                            "atm": self.instrument_mgr.calculate_atm_strike("SENSEX", ltp)
                        }
                        logger.info(f"Retrieved live AngelOne SENSEX spot quote: {ltp} (ATM: {app_state.spot_data['SENSEX']['atm']})")
            except Exception as e:
                logger.warning(f"Failed to fetch AngelOne SENSEX quote: {e}")

    async def _fetch_historical_candles(self, exchange: str, symboltoken: str, interval: str = "THREE_MINUTE") -> List[Dict[str, Any]]:
        """Fetches historical candles from AngelOne REST SmartAPI for zero-latency indicator warmup."""
        if not self.auth or not self.auth.smart_connect:
            return []
        from datetime import datetime, timedelta
        now = datetime.now()
        today_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
        if now < today_start:
            from_dt = (today_start - timedelta(days=1)).strftime("%Y-%m-%d 09:15")
            to_dt = (today_start - timedelta(days=1)).strftime("%Y-%m-%d 15:30")
        else:
            from_dt = today_start.strftime("%Y-%m-%d %H:%M")
            to_dt = now.strftime("%Y-%m-%d %H:%M")

        param = {
            "exchange": exchange,
            "symboltoken": symboltoken,
            "interval": interval,
            "fromdate": from_dt,
            "todate": to_dt
        }
        loop = asyncio.get_running_loop()
        try:
            res = await loop.run_in_executor(None, lambda: self.auth.smart_connect.getCandleData(param))
            if res and res.get("status") and res.get("data"):
                raw_candles = res["data"]
                candles = []
                for item in raw_candles:
                    if len(item) >= 5:
                        candles.append({
                            "timestamp": str(item[0]),
                            "open": float(item[1]),
                            "high": float(item[2]),
                            "low": float(item[3]),
                            "close": float(item[4]),
                            "volume": float(item[5]) if len(item) > 5 else 1000.0
                        })
                logger.info(f"Retrieved {len(candles)} historical {interval} candles for {exchange}:{symboltoken} from AngelOne REST.")
                return candles
        except Exception as e:
            logger.warning(f"Could not fetch historical candles for {exchange}:{symboltoken}: {e}")
        return []

    async def initialize(self):
        try:
            await init_db()
        except Exception as e:
            logger.warning(f"⚠️ Database initialization failed (PostgreSQL offline): {e}. Proceeding in resilient mode — live signals will stream via Redis/WebSocket.")
        await self.redis_bus.connect()
        await self.instrument_mgr.sync_master()
        if hasattr(self, "telegram") and self.telegram:
            await self.telegram.initialize()

        # Authenticate AngelOne SmartAPI with .env credentials
        auth_res = await self.auth.login()
        if auth_res.get("status"):
            await self._fetch_spot_quotes()

            # Warm-up indicators with real historical candles
            nifty_candles = await self._fetch_historical_candles("NSE", "99926000", "THREE_MINUTE")
            sensex_candles = await self._fetch_historical_candles("BSE", "99919000", "THREE_MINUTE")
            candles_map = {"NIFTY": nifty_candles, "SENSEX": sensex_candles}

            for inst, candles in candles_map.items():
                if candles:
                    for strat in self.strategies:
                        if hasattr(strat, "seed_from_candles"):
                            try:
                                strat.seed_from_candles(inst, candles)
                            except Exception as e:
                                logger.warning(f"Failed to seed candles for {strat.name} on {inst}: {e}")

        # Pre-seed strategy indicator states from spot quotes for zero-delay startup (as fallback/baseline)
        for inst in ["NIFTY", "SENSEX"]:
            spot_info = app_state.spot_data.get(inst, {})
            if spot_info.get("ltp"):
                for strat in self.strategies:
                    if hasattr(strat, "seed_from_spot"):
                        try:
                            strat.seed_from_spot(inst, spot_info)
                        except Exception as e:
                            logger.warning(f"Failed to seed {strat.name} for {inst}: {e}")

        # Wire resolution feedback loop: strategies receive outcome notifications
        for strat in self.strategies:
            if hasattr(strat, "notify_resolution"):
                self.signal_tracker.register_resolution_callback(strat.notify_resolution)
                logger.info(f"Registered resolution feedback callback for {strat.name}")

        # Build initial token map based on current spot prices
        self._update_token_cache(self.nifty_spot, self.sensex_spot)

        if auth_res.get("status"):
            self.ws_client = SmartAPIWebSocketClient(
                redis_bus=self.redis_bus,
                auth_token=self.auth.jwt_token or "",
                api_key=self.auth.api_key or "",
                client_code=self.auth.client_code or "",
                feed_token=self.auth.feed_token or ""
            )
            # Group tokens by exchange
            tokens_by_exch = {"nse_cm": ["99926000", "26000"], "bse_cm": ["99919000"], "nfo_fo": [], "bfo_fo": []}
            for t, meta in self.token_meta_cache.items():
                if not meta.get("is_spot"):
                    exch = str(meta.get("exchange", "nfo_fo")).lower()
                    if "bfo" in exch or "bse" in exch:
                        tokens_by_exch["bfo_fo"].append(t)
                    else:
                        tokens_by_exch["nfo_fo"].append(t)
            self.ws_client.set_tokens(tokens_by_exch)

        # Inject into route app_state
        app_state.redis_bus = self.redis_bus
        app_state.instrument_manager = self.instrument_mgr
        app_state.risk_governor = self.risk_governor
        app_state.signal_tracker = self.signal_tracker

    def _update_token_cache(self, nifty_spot: float, sensex_spot: float):
        nifty_wings = self.instrument_mgr.get_atm_and_wings("NIFTY", nifty_spot)
        sensex_wings = self.instrument_mgr.get_atm_and_wings("SENSEX", sensex_spot)

        self.token_meta_cache.clear()
        # Add spot tokens
        self.token_meta_cache["99926000"] = {"is_spot": True, "name": "NIFTY", "exchange": "nse_cm"}
        self.token_meta_cache["26000"] = {"is_spot": True, "name": "NIFTY", "exchange": "nse_cm"}
        self.token_meta_cache["99919000"] = {"is_spot": True, "name": "SENSEX", "exchange": "bse_cm"}

        # Add options tokens
        for t, meta in nifty_wings.get("tokens", {}).items():
            self.token_meta_cache[str(t)] = meta
        for t, meta in sensex_wings.get("tokens", {}).items():
            self.token_meta_cache[str(t)] = meta

    async def start_workers(self):
        self.running = True
        if self.ws_client:
            await self.ws_client.start()
            logger.info("AngelOne SmartAPI WebSocket 2.0 client started.")
        if self.chain_poller:
            await self.chain_poller.start()
        self.tasks.append(asyncio.create_task(self._tick_consumer_loop()))
        self.tasks.append(asyncio.create_task(self._throttle_broadcast_loop()))
        self.tasks.append(asyncio.create_task(self._signals_listener_loop()))
        logger.info("Alpha 2.0 Engine background workers started.")

    async def stop_workers(self):
        self.running = False
        if self.chain_poller:
            await self.chain_poller.stop()
        if self.ws_client:
            await self.ws_client.stop()
        for t in self.tasks:
            t.cancel()
        if hasattr(self, "telegram") and self.telegram:
            await self.telegram.close()
        await self.redis_bus.close()
        logger.info("Alpha 2.0 Engine stopped.")

    async def _tick_consumer_loop(self):
        """
        Consumes ticks from Redis market_channel and simulated_channel.
        Processes spot migrations, strategy triggers, and signal tracking.
        """
        channels = [self.redis_bus.market_channel, self.redis_bus.simulated_channel]
        async for tick in self.redis_bus.subscribe(*channels):
            if not self.running:
                break

            token = str(tick.get("token", ""))
            ltp = float(tick.get("ltp", 0.0))
            if ltp <= 0:
                continue

            # Store in latest buffer for 250ms WebSocket throttle
            app_state.latest_ticks_buffer[token] = tick

            meta = self.token_meta_cache.get(token)

            # Feed option tick to chain poller for aggregate PCR and Max Pain tracking
            if meta and not meta.get("is_spot") and meta.get("strike") and meta.get("option_type"):
                raw_oi = float(tick.get("open_interest", 0.0))
                self.chain_poller.record_tick(
                    inst=meta.get("name", "NIFTY"),
                    token=token,
                    strike=float(meta.get("strike")),
                    opt_type=str(meta.get("option_type")),
                    oi=raw_oi,
                    ltp=ltp,
                    ts=float(tick.get("exchange_timestamp", 0.0)) or datetime.now(timezone.utc).timestamp()
                )

            # Spot price migration & live spot update detection
            is_spot_tick = (meta and meta.get("is_spot")) or token in ("99926000", "26000", "99919000")
            if is_spot_tick and ltp > 0:
                inst = meta.get("name") if meta else ("NIFTY" if token in ("99926000", "26000") else "SENSEX")
                if inst in app_state.spot_data:
                    spot_entry = app_state.spot_data[inst]
                    spot_entry["ltp"] = ltp
                    if tick.get("close", 0) > 0:
                        spot_entry["close"] = float(tick["close"])
                    close_val = spot_entry.get("close", ltp)
                    spot_entry["change"] = round(ltp - close_val, 2)
                    spot_entry["change_pct"] = round((spot_entry["change"] / close_val) * 100, 2) if close_val > 0 else 0.0
                    if tick.get("high", 0) > 0:
                        spot_entry["high"] = float(tick["high"])
                    if tick.get("low", 0) > 0:
                        spot_entry["low"] = float(tick["low"])
                    if tick.get("open", 0) > 0:
                        spot_entry["open"] = float(tick["open"])

                    atm = self.instrument_mgr.calculate_atm_strike(inst, ltp)
                    spot_entry["atm"] = atm
                    self.instrument_mgr.current_atm[inst] = atm

                    if inst == "NIFTY":
                        self.nifty_spot = ltp
                    else:
                        self.sensex_spot = ltp

                    migrated_atm = self.instrument_mgr.detect_atm_migration(inst, ltp)
                    if migrated_atm:
                        logger.info(f"ATM migration detected for {inst}: new ATM is {migrated_atm}")
                        self._update_token_cache(self.nifty_spot, self.sensex_spot)
                        if self.ws_client:
                            tokens_by_exch = {"nse_cm": ["99926000", "26000"], "bse_cm": ["99919000"], "nfo_fo": [], "bfo_fo": []}
                            for t, m in self.token_meta_cache.items():
                                if not m.get("is_spot"):
                                    exch = str(m.get("exchange", "nfo_fo")).lower()
                                    if "bfo" in exch or "bse" in exch:
                                        tokens_by_exch["bfo_fo"].append(t)
                                    else:
                                        tokens_by_exch["nfo_fo"].append(t)
                            await self.ws_client.update_subscriptions(tokens_by_exch)

            # 1. Update Signal Tracker
            await self.signal_tracker.on_tick(tick)

            # 2. Evaluate Strategies
            for strat in self.strategies:
                try:
                    candidate = await strat.on_tick(tick, meta=meta)
                    if candidate:
                        await self._process_candidate_signal(candidate)

                    # Broadcast pre-move radar alerts
                    while getattr(strat, "pending_alerts", None) and len(strat.pending_alerts) > 0:
                        alert = strat.pending_alerts.popleft()
                        await self.redis_bus.publish_signal({
                            "event": "RADAR_PRE_ALERT",
                            "alert": alert
                        })
                except Exception as e:
                    logger.error(f"Strategy {strat.name} error on tick: {e}")


    async def _process_candidate_signal(self, candidate: Dict[str, Any]):
        # Pass through Master Risk Governor
        evaluated_sig = self.risk_governor.evaluate_signal(candidate)
        if not evaluated_sig:
            return

        # Save to PostgreSQL
        try:
            async with AsyncSessionLocal() as session:
                details_payload = {
                    "confidence": evaluated_sig.get("confidence"),
                    "target_1r": evaluated_sig.get("target_1r"),
                    "target_2r": evaluated_sig.get("target_2r"),
                    "custom_sl_spot": evaluated_sig.get("custom_sl_spot"),
                    "partial_exit_guidance": evaluated_sig.get("partial_exit_guidance"),
                    "risk_meta": evaluated_sig.get("risk_meta", {}),
                    **evaluated_sig.get("details", {})
                }
                db_sig = Signal(
                    signal_id=evaluated_sig["signal_id"],
                    instrument=evaluated_sig["instrument"],
                    strategy=evaluated_sig["strategy"],
                    direction=evaluated_sig["direction"],
                    strike=evaluated_sig["strike"],
                    option_type=evaluated_sig["option_type"],
                    option_token=evaluated_sig["option_token"],
                    option_symbol=evaluated_sig["option_symbol"],
                    spot_entry=evaluated_sig["spot_entry"],
                    entry_price=evaluated_sig["entry_price"],
                    stop_loss=evaluated_sig["stop_loss"],
                    target=evaluated_sig["target"],
                    lot_size=evaluated_sig["lot_size"],
                    quantity=evaluated_sig["quantity"],
                    risk_amount=evaluated_sig["risk_amount"],
                    status="ACTIVE",
                    details=details_payload
                )
                session.add(db_sig)
                await session.commit()
        except Exception as e:
            logger.error(f"Failed to persist signal to database: {e}")

        # Register with active tracker
        self.signal_tracker.register_signal(evaluated_sig)

        # Broadcast signal to Redis and WebSocket clients
        await self.redis_bus.publish_signal({
            "event": "NEW_SIGNAL",
            "signal": evaluated_sig
        })

    async def _signals_listener_loop(self):
        """Listens to channel:signals and pushes immediately to WebSocket clients and Telegram"""
        async for msg in self.redis_bus.subscribe(self.redis_bus.signals_channel):
            if not self.running:
                break
            payload = json.dumps(msg)
            for ws in list(app_state.connected_websockets):
                try:
                    await ws.send_text(payload)
                except Exception:
                    pass

            # Telegram dispatch for signals, precursor radar alerts, and trade resolutions
            if hasattr(self, "telegram") and self.telegram and self.telegram.enabled:
                try:
                    evt = msg.get("event")
                    if evt == "NEW_SIGNAL" and "signal" in msg:
                        await self.telegram.notify_new_signal(msg["signal"])
                    elif evt == "RADAR_PRE_ALERT" and "alert" in msg:
                        await self.telegram.notify_radar_alert(msg["alert"])
                    elif evt == "SIGNAL_RESOLVED" and "signal" in msg:
                        await self.telegram.notify_signal_resolved(msg["signal"])
                except Exception as e:
                    logger.warning(f"Telegram dispatch error in signals listener: {e}")

    async def _throttle_broadcast_loop(self):
        """
        Enforces strict 250ms WebSocket throttle:
        Batches buffered ticks and telemetry, broadcasting at 4 Hz.
        """
        while self.running:
            await asyncio.sleep(0.25) # 250 milliseconds
            if not app_state.connected_websockets:
                continue

            ticks_to_send = dict(app_state.latest_ticks_buffer)
            app_state.latest_ticks_buffer.clear()

            # Active signals snapshot with updated live LTP and deviation
            active_cards = list(self.signal_tracker.active_signals.values())

            batch_payload = json.dumps({
                "type": "TICK_BATCH",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "ticks": ticks_to_send,
                "active_signals": active_cards,
                "spot_data": app_state.spot_data
            })

            for ws in list(app_state.connected_websockets):
                try:
                    await ws.send_text(batch_payload)
                except Exception:
                    pass

coordinator = EngineCoordinator()

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Alpha 2.0 Backend Engine...")
    await coordinator.initialize()
    await coordinator.start_workers()
    yield
    logger.info("Shutting down Alpha 2.0 Backend Engine...")
    await coordinator.stop_workers()

def create_app() -> FastAPI:
    app = FastAPI(
        title="Project Alpha 2.0 Advisory & Backtesting Harness",
        version="2.0.0",
        lifespan=lifespan
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)
    return app

app = create_app()
