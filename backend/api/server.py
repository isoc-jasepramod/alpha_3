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
from backend.strategies.simplified_engine import SimplifiedPriceActionEngine
from backend.strategies.regime_filter import RegimeFilter
from backend.strategies.indicators import CandleAggregator, IncrementalVWAP, IncrementalADX
from backend.strategies.iv_engine import IVEngine
from backend.strategies.gex_engine import GEXEngine
from backend.strategies.flow_engine import FlowEngine
from backend.strategies.flow_engine_v2 import FlowEngineV2
from backend.strategies.squeeze_detector import SqueezeDetector
from backend.core.chain_poller import OptionChainPoller
from backend.core.telegram_notifier import TelegramNotifier
from backend.api.routes import router, app_state

class EngineCoordinator:
    # Minimum historical candles required to consider indicators "warm" (EMA21 needs 21;
    # ADX14 settles by ~28). Below this, warmup widens its lookback and finally falls back
    # to locally recorded sessions — the system must never start cold.
    MIN_WARMUP_CANDLES = 30

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

        # Shared Market Regime Filter
        self.regime_filter = RegimeFilter(strat_cfg.get("regime_filter"))

        # Independent Regime Indicators & Candle Aggregators (driven directly by spot ticks)
        self.regime_aggregators: Dict[str, CandleAggregator] = {
            "NIFTY": CandleAggregator(timeframe_seconds=180),
            "SENSEX": CandleAggregator(timeframe_seconds=180)
        }
        self.regime_vwap: Dict[str, IncrementalVWAP] = {
            "NIFTY": IncrementalVWAP(),
            "SENSEX": IncrementalVWAP()
        }
        self.regime_adx: Dict[str, IncrementalADX] = {
            "NIFTY": IncrementalADX(period=14),
            "SENSEX": IncrementalADX(period=14)
        }

        # Core and Precursor Predictive Engines
        iv_engine = IVEngine(strat_cfg.get("iv_engine"))
        gex_engine = GEXEngine(iv_engine=iv_engine, config=strat_cfg.get("gex_engine"))
        self.gex_engine = gex_engine
        app_state.gex_engine = gex_engine
        flow_engine = FlowEngine(strat_cfg.get("flow_engine"))
        flow_v2_engine = FlowEngineV2(strat_cfg.get("flow_v2"))
        squeeze_detector = SqueezeDetector(strat_cfg.get("squeeze_detector"))

        self.chain_poller = OptionChainPoller(
            redis_bus=self.redis_bus,
            instrument_mgr=self.instrument_mgr,
            poll_interval_sec=60
        )

        vwap_ema_strat = VWAPEMAAlignment(strat_cfg.get("vwap_ema"), regime_filter=self.regime_filter)
        momentum_strat = MomentumImpulseDetector(strat_cfg.get("momentum_impulse"), regime_filter=self.regime_filter)
        simplified_strat = SimplifiedPriceActionEngine(strat_cfg.get("simplified_price_action"))

        # Strategy instances with configured rules
        self.strategies = [
            OISqueezeSentinel(strat_cfg.get("oi_squeeze")),
            VolumeBackedORB(strat_cfg.get("orb_breakout")),
            vwap_ema_strat,
            ExpiryDayGammaScalp(strat_cfg.get("gamma_scalp")),
            momentum_strat,
            simplified_strat,
            iv_engine,
            gex_engine,
            flow_engine,
            flow_v2_engine,
            squeeze_detector
        ]
        self.flow_v2_engine = flow_v2_engine  # estimated order-flow (disabled until validated)


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
                "atm": 23350.0,
                "gamma": {"net_gex": 0.0, "regime": "NEUTRAL", "call_wall": 0.0, "put_wall": 0.0}
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
                "atm": 74300.0,
                "gamma": {"net_gex": 0.0, "regime": "NEUTRAL", "call_wall": 0.0, "put_wall": 0.0}
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
        loop = asyncio.get_running_loop()

        # NO COLD START: progressively widen the lookback until we have enough candles to
        # actually warm EMA21/ADX14/RSI14. A fixed window can underflow on weekends/holiday
        # clusters or a degenerate API response (the 1-candle bug that started strategies
        # blind). AngelOne returns only real trading candles, so widening the calendar window
        # transparently reaches back past any number of non-trading days to the last sessions.
        min_candles = getattr(self, "MIN_WARMUP_CANDLES", 30)  # EMA21 needs 21, ADX14 ~28 to settle
        best: List[Dict[str, Any]] = []
        for lookback_days in (5, 10, 20, 40):
            from_dt = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d 09:15")
            to_dt = now.strftime("%Y-%m-%d %H:%M")
            param = {
                "exchange": exchange,
                "symboltoken": symboltoken,
                "interval": interval,
                "fromdate": from_dt,
                "todate": to_dt,
            }
            try:
                res = await loop.run_in_executor(
                    None, lambda: self.auth.smart_connect.getCandleData(param)
                )
                candles = []
                if res and res.get("status") and res.get("data"):
                    for item in res["data"]:
                        if len(item) >= 5:
                            candles.append({
                                "timestamp": str(item[0]),
                                "open": float(item[1]),
                                "high": float(item[2]),
                                "low": float(item[3]),
                                "close": float(item[4]),
                                "volume": float(item[5]) if len(item) > 5 else 1000.0,
                            })
                if len(candles) > len(best):
                    best = candles
                if len(candles) >= min_candles:
                    logger.info(
                        f"Retrieved {len(candles)} historical {interval} candles for "
                        f"{exchange}:{symboltoken} (lookback {lookback_days}d). Indicators WARM."
                    )
                    return candles
                logger.warning(
                    f"Warmup underflow for {exchange}:{symboltoken}: only {len(candles)} "
                    f"candles at {lookback_days}d lookback (need >= {min_candles}). Widening..."
                )
            except Exception as e:
                logger.warning(
                    f"Historical fetch error for {exchange}:{symboltoken} at {lookback_days}d: {e}. Retrying wider..."
                )
            await asyncio.sleep(0.5)  # gentle spacing to avoid REST rate limits between retries

        # Exhausted all lookbacks without reaching the minimum — return the best we got but
        # make the degraded state LOUD so a cold/partial start is never silent.
        if best:
            logger.error(
                f"⚠️ WARMUP DEGRADED for {exchange}:{symboltoken}: only {len(best)} candles after "
                f"widening to 40d (need >= {min_candles}). Indicators may start under-warmed."
            )
        else:
            logger.error(
                f"🚨 WARMUP FAILED for {exchange}:{symboltoken}: 0 candles after all retries. "
                f"Indicators would start COLD — strategies relying on EMA21/ADX14/VWAP will be blind."
            )
        return best

    def _warmup_from_local_lake(self, inst: str, min_candles: int = 30) -> List[Dict[str, Any]]:
        """
        LAST-RESORT warmup source so we NEVER start cold: if the AngelOne REST history is
        unavailable, rebuild 3-minute candles from the most recent recorded session in the
        local data lake (the tick recorder writes spot ticks daily). Returns [] only if there
        is genuinely no recorded spot data at all.
        """
        import glob
        from datetime import datetime, timezone, timedelta
        try:
            import pyarrow.parquet as pq
        except Exception:
            return []

        spot_tokens = {"NIFTY": ("26000", "99926000"), "SENSEX": ("99919000",)}.get(inst, ())
        if not spot_tokens:
            return []
        lake_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "lake")
        day_dirs = sorted(glob.glob(os.path.join(lake_dir, "date=*")), reverse=True)
        IST = timezone(timedelta(hours=5, minutes=30))

        for day_dir in day_dirs:
            ticks = []  # (ts_seconds, ltp)
            for fp in sorted(glob.glob(os.path.join(day_dir, "ticks_*.parquet"))):
                try:
                    have = set(pq.read_table(fp).schema.names)
                    cols = [c for c in ("token", "ltp", "exchange_timestamp") if c in have]
                    d = pq.read_table(fp, columns=cols).to_pydict()
                    for i in range(len(d.get("token", []))):
                        if str(d["token"][i]) in spot_tokens:
                            ts = float(d["exchange_timestamp"][i])
                            ts = ts / 1000.0 if ts > 1e11 else ts
                            lt = float(d["ltp"][i])
                            if lt > 0:
                                ticks.append((ts, lt))
                except Exception:
                    continue
            if len(ticks) < 50:
                continue  # not a real session; try an older day
            ticks.sort(key=lambda x: x[0])
            # aggregate into 3-minute OHLC candles, trading hours only (09:15-15:30 IST)
            candles = []
            cur = None
            for ts, lt in ticks:
                dt = datetime.fromtimestamp(ts, tz=IST)
                if dt.hour < 9 or (dt.hour == 9 and dt.minute < 15) or dt.hour > 15 or (dt.hour == 15 and dt.minute > 30):
                    continue
                slot = int(ts // 180) * 180
                if cur is None or slot != cur["_slot"]:
                    if cur is not None:
                        candles.append({k: cur[k] for k in ("timestamp", "open", "high", "low", "close", "volume")})
                    cur = {"_slot": slot, "timestamp": datetime.fromtimestamp(slot, tz=IST).isoformat(),
                           "open": lt, "high": lt, "low": lt, "close": lt, "volume": 1000.0}
                else:
                    cur["high"] = max(cur["high"], lt)
                    cur["low"] = min(cur["low"], lt)
                    cur["close"] = lt
            if cur is not None:
                candles.append({k: cur[k] for k in ("timestamp", "open", "high", "low", "close", "volume")})
            if len(candles) >= min_candles:
                logger.warning(
                    f"🩹 [WARMUP FALLBACK] {inst}: rebuilt {len(candles)} 3m candles from local "
                    f"recorded session {os.path.basename(day_dir)} (REST history unavailable)."
                )
                return candles
        return []

    async def initialize(self):
        try:
            await init_db()
            await self.signal_tracker.restore_from_db()
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

        # Warmup ALWAYS runs (even if auth failed): the REST fetch returns [] without a live
        # session and the local-tick fallback then supplies candles, so we never skip warmup
        # silently. This block is intentionally outside the auth gate.
        if True:
            # Warm-up indicators with real historical candles (empty if REST unavailable)
            nifty_candles = await self._fetch_historical_candles("NSE", "99926000", "THREE_MINUTE") if auth_res.get("status") else []
            sensex_candles = await self._fetch_historical_candles("BSE", "99919000", "THREE_MINUTE") if auth_res.get("status") else []
            candles_map = {"NIFTY": nifty_candles, "SENSEX": sensex_candles}

            # NO COLD START guarantee: if REST history couldn't supply enough candles for an
            # instrument, rebuild warmup from the most recent locally-recorded session.
            min_candles = getattr(self, "MIN_WARMUP_CANDLES", 30)
            warmup_source = {}  # inst -> "LIVE_REST" | "LOCAL_TICKS" | "INSUFFICIENT"
            for inst in ("NIFTY", "SENSEX"):
                rest_n = len(candles_map.get(inst) or [])
                warmup_source[inst] = "LIVE_REST" if rest_n >= min_candles else None
                if rest_n < min_candles:
                    fallback = self._warmup_from_local_lake(inst, min_candles)
                    if len(fallback) > rest_n:
                        candles_map[inst] = fallback
                        warmup_source[inst] = "LOCAL_TICKS"
                final_n = len(candles_map.get(inst) or [])
                if final_n < min_candles:
                    warmup_source[inst] = "INSUFFICIENT"
                    logger.error(
                        f"🚨 [COLD START RISK] {inst}: only {final_n} warmup candles from "
                        f"REST+local fallback (need >= {min_candles}). Trend/regime indicators "
                        f"will under-warm until live candles accumulate."
                    )

            # Single authoritative summary so the warmup source is unambiguous in the log.
            _src_label = {
                "LIVE_REST": "✅ LIVE historical REST data",
                "LOCAL_TICKS": "🩹 LOCAL recorded tick data (REST unavailable/insufficient)",
                "INSUFFICIENT": "🚨 INSUFFICIENT — starting under-warmed (COLD RISK)",
            }
            logger.info("──────── WARMUP SUMMARY ────────")
            for inst in ("NIFTY", "SENSEX"):
                src = warmup_source.get(inst, "INSUFFICIENT")
                n = len(candles_map.get(inst) or [])
                first_ts = (candles_map[inst][0]["timestamp"][:16] if candles_map.get(inst) else "-")
                last_ts = (candles_map[inst][-1]["timestamp"][:16] if candles_map.get(inst) else "-")
                logger.info(
                    f"   {inst}: {n} candles  source={_src_label.get(src, src)}  span[{first_ts} → {last_ts}]"
                )
            overall = ("WARM" if all(warmup_source.get(i) in ("LIVE_REST", "LOCAL_TICKS")
                                     for i in ("NIFTY", "SENSEX")) else "DEGRADED")
            logger.info(f"──────── WARMUP {overall} ────────")

            for inst, candles in candles_map.items():
                if candles:
                    last_c = candles[-1]
                    last_close = float(last_c.get("close", 0.0))
                    if last_close > 0:
                        if inst in app_state.spot_data:
                            app_state.spot_data[inst]["ltp"] = last_close
                            app_state.spot_data[inst]["close"] = last_close
                            app_state.spot_data[inst]["atm"] = self.instrument_mgr.calculate_atm_strike(inst, last_close)
                        if inst == "NIFTY":
                            self.nifty_spot = last_close
                        elif inst == "SENSEX":
                            self.sensex_spot = last_close

                    try:
                        self.regime_filter.seed_from_candles(inst, candles)
                        for c in candles:
                            h = float(c.get("high", 0.0))
                            l = float(c.get("low", 0.0))
                            cl = float(c.get("close", 0.0))
                            v = float(c.get("volume", 1000.0))
                            if cl <= 0:
                                continue
                            typical = (h + l + cl) / 3.0
                            if inst in self.regime_vwap:
                                self.regime_vwap[inst].update(typical, v)
                            if inst in self.regime_adx:
                                self.regime_adx[inst].update(h, l, cl)
                            self.risk_governor.on_spot_candle(inst, h, l, cl)
                    except Exception as e:
                        logger.warning(f"Failed to seed regime filter for {inst}: {e}")
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

        # Build initial token map based on current spot prices.
        # HARDENING: before 09:15 the AngelOne LTP quote can be a STALE pre-open print (this is
        # how NIFTY subscribed strikes off ~22183 and SENSEX off ~74081 while the real opens were
        # ~22518 / ~72259 — then the migration bug froze those wrong strikes all day). Prefer the
        # last warmup candle's close as the spot anchor for the INITIAL strike band, since it is a
        # real traded price; fall back to the LTP quote only if no candles are available. The ATM
        # migration logic then keeps strikes tracking spot once the live session begins.
        nifty_anchor = self._anchor_spot_for_subscription("NIFTY", candles_map, self.nifty_spot)
        sensex_anchor = self._anchor_spot_for_subscription("SENSEX", candles_map, self.sensex_spot)
        self.nifty_spot = nifty_anchor or self.nifty_spot
        self.sensex_spot = sensex_anchor or self.sensex_spot
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
        app_state.regime_filter = self.regime_filter

    def _anchor_spot_for_subscription(self, inst: str, candles_map: Dict[str, Any], ltp_spot: float) -> float:
        """
        Pick a RELIABLE spot anchor for the INITIAL option-strike subscription.
        Prefers the last warmup candle close (a real traded price) over the startup LTP quote,
        which pre-09:15 can be a stale pre-open print that subscribes the wrong strike band.
        Falls back to the LTP quote if candles are missing or look inconsistent.
        """
        candles = (candles_map or {}).get(inst) or []
        candle_close = 0.0
        if candles:
            try:
                candle_close = float(candles[-1].get("close", 0.0))
            except (TypeError, ValueError, AttributeError):
                candle_close = 0.0

        ltp_spot = float(ltp_spot or 0.0)

        # If we have a sane candle close, prefer it. Sanity: positive, and (when both exist)
        # within 3% of the LTP quote so a corrupt candle can't pick an absurd strike band.
        if candle_close > 0:
            if ltp_spot <= 0 or abs(candle_close - ltp_spot) / candle_close <= 0.03:
                if ltp_spot > 0 and abs(candle_close - ltp_spot) / candle_close > 0.003:
                    logger.info(
                        f"🔧 [SUBSCRIBE ANCHOR] {inst}: using warmup candle close {candle_close:.1f} "
                        f"instead of startup LTP {ltp_spot:.1f} (likely pre-open) for initial strikes."
                    )
                return candle_close
            # candle and LTP disagree by >3%: trust whichever is non-stale is ambiguous, so keep
            # LTP but log loudly — migration will correct within minutes of the real open.
            logger.warning(
                f"⚠️ [SUBSCRIBE ANCHOR] {inst}: warmup close {candle_close:.1f} and startup LTP "
                f"{ltp_spot:.1f} differ >3%. Using LTP; ATM migration will correct post-open."
            )
        return ltp_spot if ltp_spot > 0 else candle_close

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
        self.tasks.append(asyncio.create_task(self._eod_sweep_scheduler()))
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

    async def _eod_sweep_scheduler(self):
        """
        Background scheduler that triggers at 15:25 IST every trading day.
        Sweeps all ACTIVE signals, marks them EOD_EXPIRED, records last LTP
        as exit price, persists PnL to database, and sends a Telegram summary.
        Also resets the Risk Governor daily PnL counter.
        """
        from datetime import timedelta
        IST = timezone(timedelta(hours=5, minutes=30))

        while self.running:
            try:
                now_ist = datetime.now(IST)
                # Calculate seconds until next 15:25:00 IST
                target_today = now_ist.replace(hour=15, minute=25, second=0, microsecond=0)
                if now_ist >= target_today:
                    # Already past 15:25 today — schedule for tomorrow
                    target_today += timedelta(days=1)
                    # Skip weekends
                    while target_today.weekday() >= 5:
                        target_today += timedelta(days=1)

                wait_seconds = (target_today - now_ist).total_seconds()
                logger.info(f"⏰ [EOD SCHEDULER] Next sweep at {target_today.strftime('%Y-%m-%d %H:%M IST')} ({wait_seconds / 3600:.1f}h from now)")
                await asyncio.sleep(wait_seconds)

                if not self.running:
                    break

                logger.warning("🏁 [EOD SWEEP] Market closing in 5 minutes — sweeping active signals...")

                # 1. Sweep all active signals
                expired = await self.signal_tracker.sweep_eod_signals()

                # 2. Send Telegram summary
                if hasattr(self, "telegram") and self.telegram and self.telegram.enabled:
                    try:
                        if expired:
                            total_pnl = sum(s.get("theoretical_pnl", 0) for s in expired)
                            lines = [f"🏁 <b>END-OF-DAY SWEEP ({len(expired)} signals expired)</b>\n"]
                            for s in expired:
                                sym = s.get("option_symbol", "?")
                                pnl = float(s.get("theoretical_pnl", 0))
                                entry = float(s.get("entry_price", 0))
                                exit_p = float(s.get("exit_price", 0))
                                icon = "📈" if pnl >= 0 else "📉"
                                lines.append(f"{icon} <code>{sym}</code>: ₹{entry:.2f} → ₹{exit_p:.2f} = <b>₹{pnl:+,.2f}</b>")
                            lines.append(f"\n💰 <b>EOD Total PnL: ₹{total_pnl:+,.2f}</b>")
                            await self.telegram.enqueue_message("\n".join(lines))
                        else:
                            await self.telegram.enqueue_message("🏁 <b>END-OF-DAY:</b> No active signals at market close. Clean session.")
                    except Exception as e:
                        logger.error(f"Telegram EOD summary error: {e}")

                # 3. Reset daily PnL for next session
                self.risk_governor.realized_daily_pnl = 0.0
                self.risk_governor.circuit_breaker_tripped = False
                self.risk_governor.circuit_breaker_time = None
                logger.info("🔄 [EOD RESET] Risk Governor daily state reset for next session.")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"EOD sweep scheduler error: {e}")
                await asyncio.sleep(60)  # Retry in 1 min if something fails

    def _drive_regime(self, inst: str, ltp: float, tick: Dict[str, Any]):
        """
        Independently aggregates spot ticks into 3m candles and feeds RegimeFilter.
        Operates directly in EngineCoordinator decoupled from any individual strategy.
        """
        if inst not in self.regime_aggregators:
            return

        try:
            raw_ts = tick.get("exchange_timestamp", 0.0) or datetime.now(timezone.utc).timestamp()
            ts = float(raw_ts) if float(raw_ts) < 1e11 else float(raw_ts) / 1000.0
            vol = float(tick.get("volume", 1.0)) or 1.0

            if inst in self.regime_vwap:
                self.regime_vwap[inst].update(ltp, vol)

            closed = self.regime_aggregators[inst].on_tick(ts, ltp, volume=vol)
            if closed:
                h = float(closed.get("high", ltp))
                l = float(closed.get("low", ltp))
                c = float(closed.get("close", ltp))

                vwap_val = self.regime_vwap[inst].value if inst in self.regime_vwap else c
                # Self-healing sanity anchor: an index VWAP cannot mathematically deviate > 2% from candle close
                if c > 0 and abs(vwap_val - c) / c > 0.02:
                    if inst in self.regime_vwap:
                        self.regime_vwap[inst].seed(c, 5000.0)
                    vwap_val = c

                adx_val = self.regime_adx[inst].update(h, l, c) if inst in self.regime_adx else 20.0

                self.regime_filter.update_candle(inst, closed, vwap_val, adx_val)
                self.risk_governor.on_spot_candle(inst, h, l, c)
        except Exception as e:
            logger.error(f"Error updating independent regime for {inst}: {e}")

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
                raw_exch_ts = float(tick.get("exchange_timestamp", 0.0) or 0.0)
                ts_norm = (raw_exch_ts if raw_exch_ts < 1e11 else raw_exch_ts / 1000.0) if raw_exch_ts > 0 else datetime.now(timezone.utc).timestamp()
                self.chain_poller.record_tick(
                    inst=meta.get("name", "NIFTY"),
                    token=token,
                    strike=float(meta.get("strike")),
                    opt_type=str(meta.get("option_type")),
                    oi=raw_oi,
                    ltp=ltp,
                    ts=ts_norm
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
                    spot_entry["atm"] = atm  # for UI/telemetry display only
                    # DO NOT set instrument_mgr.current_atm here: detect_atm_migration() compares
                    # the live ATM against current_atm to decide if a migration occurred, and it
                    # updates current_atm itself when one does. Writing current_atm = atm on every
                    # tick (as before) made that comparison always equal -> migration NEVER fired,
                    # freezing option subscriptions at the startup ATM even as spot moved far away
                    # (e.g. SENSEX subscribed ~74000 at the gap-up open, traded all day at ~72200
                    # on stale ~1700pt-OTM strikes). Let detect_atm_migration own current_atm.

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

                    # Independently drive session market regime filter from spot ticks
                    self._drive_regime(inst, ltp, tick)

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

            # Attach live gamma profiles to spot_data
            if hasattr(self, "gex_engine") and self.gex_engine:
                for inst in ("NIFTY", "SENSEX"):
                    if inst in app_state.spot_data:
                        app_state.spot_data[inst]["gamma"] = self.gex_engine.get_gamma_profile(inst)

            batch_payload = json.dumps({
                "type": "TICK_BATCH",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "ticks": ticks_to_send,
                "active_signals": active_cards,
                "spot_data": app_state.spot_data,
                "gamma": self.gex_engine.latest_gex if hasattr(self, "gex_engine") and self.gex_engine else {},
                "regimes": {
                    "NIFTY": self.regime_filter.get_regime("NIFTY"),
                    "SENSEX": self.regime_filter.get_regime("SENSEX")
                }
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
