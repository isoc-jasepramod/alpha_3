import os
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from loguru import logger
import yaml

IST = timezone(timedelta(hours=5, minutes=30))

DEFAULT_SCHEDULE = ["09:15", "10:00", "10:45", "11:30", "12:15", "13:00", "13:45", "14:30", "15:15"]


class PeriodicAnalysisReporter:
    """
    Intraday Periodic Market & Signals Digest Reporter for Alpha 3.0.
    Executes every 45 minutes starting from 09:15 AM IST during market hours.
    Compiles:
      - Live Spot & Range Telemetry (NIFTY & SENSEX)
      - Session VWAP & EMA Alignment
      - Trend Regime & Market Direction
      - Option Chain Telemetry (PCR, MaxPain, GEX)
      - Cumulative Signal Tracker Performance & Net PnL
      - Tactical Actionable Guidance for the upcoming 45-minute window
    Dispatches rich HTML reports directly to Telegram.
    """

    def __init__(self, server: Any, config_path: Optional[str] = None):
        self.server = server
        self.enabled = True
        self.interval_minutes = 45
        self.schedule_times = list(DEFAULT_SCHEDULE)
        self.dispatched_slots: set = set() # Track "YYYY-MM-DD HH:MM" already sent
        self._task: Optional[asyncio.Task] = None
        self.running = False

        self._load_config(config_path)

    def _load_config(self, config_path: Optional[str] = None):
        cfg_path = config_path or os.path.join(os.path.dirname(__file__), "..", "..", "config", "settings.yaml")
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, "r") as f:
                    t_cfg = yaml.safe_load(f).get("telegram", {}).get("periodic_reports", {})
                    self.enabled = bool(t_cfg.get("enabled", self.enabled))
                    self.interval_minutes = int(t_cfg.get("interval_minutes", self.interval_minutes))
                    custom_sched = t_cfg.get("schedule_times")
                    if custom_sched and isinstance(custom_sched, list):
                        self.schedule_times = [str(x).strip() for x in custom_sched]
            except Exception as e:
                logger.warning(f"Error reading periodic report settings: {e}")

    def build_digest_payload(self) -> Dict[str, Any]:
        """Gathers snapshot metrics from all server components."""
        now_dt = datetime.now(IST)
        today_str = now_dt.strftime("%Y-%m-%d")

        # 1. Spot Telemetry
        spot_data = getattr(self.server, "app_state", None)
        spots = getattr(spot_data, "spot_data", {}) if spot_data else {}
        
        nifty_info = spots.get("NIFTY", {})
        sensex_info = spots.get("SENSEX", {})

        nifty_p = float(nifty_info.get("ltp") or getattr(self.server, "nifty_spot", 0.0))
        sensex_p = float(sensex_info.get("ltp") or getattr(self.server, "sensex_spot", 0.0))

        # 2. VWAP
        regime_vwaps = getattr(self.server, "regime_vwap", {})
        nifty_vwap = regime_vwaps.get("NIFTY").value if regime_vwaps.get("NIFTY") else nifty_p
        sensex_vwap = regime_vwaps.get("SENSEX").value if regime_vwaps.get("SENSEX") else sensex_p

        # 3. Regime Filter
        rf = getattr(self.server, "regime_filter", None)
        nifty_regime = rf.regimes.get("NIFTY") if rf else None
        sensex_regime = rf.regimes.get("SENSEX") if rf else None

        # 4. Option Chain Metrics
        cp = getattr(self.server, "chain_poller", None)
        nifty_chain = cp.chain_metrics.get("NIFTY", {}) if cp else {}
        sensex_chain = cp.chain_metrics.get("SENSEX", {}) if cp else {}

        # 5. Signal Tracker & Performance
        st = getattr(self.server, "signal_tracker", None)
        active_sigs = list(st.active_signals.values()) if st else []
        active_running = [s for s in active_sigs if s.get("status") in ("ACTIVE", None)]

        rg = getattr(self.server, "risk_governor", None)
        realized_pnl = float(getattr(rg, "realized_daily_pnl", 0.0)) if rg else 0.0

        # Calculate today's resolved signals stats
        target_hits = sum(1 for s in active_sigs if s.get("status") == "TARGET_HIT")
        stop_hits = sum(1 for s in active_sigs if s.get("status") == "STOP_HIT")
        chase_blocked = sum(1 for s in active_sigs if s.get("status") == "INVALID_CHASE_PREVENTED")
        theoretical_pnl = sum(float(s.get("theoretical_pnl", 0.0)) for s in active_sigs)

        # 6. GEX Engine
        gex = getattr(self.server, "gex_engine", None)
        nifty_gex = gex.get_current_metrics("NIFTY") if gex and hasattr(gex, "get_current_metrics") else {}
        sensex_gex = gex.get_current_metrics("SENSEX") if gex and hasattr(gex, "get_current_metrics") else {}

        return {
            "timestamp": now_dt,
            "nifty": {
                "ltp": nifty_p,
                "open": float(nifty_info.get("open", nifty_p)),
                "high": float(nifty_info.get("high", nifty_p)),
                "low": float(nifty_info.get("low", nifty_p)),
                "vwap": nifty_vwap,
                "change_pct": float(nifty_info.get("change_pct", 0.0)),
                "regime": nifty_regime.state.name if nifty_regime else "BALANCED_RANGE",
                "regime_score": nifty_regime.trend_score if nifty_regime else 50.0,
                "adx": nifty_regime.adx if nifty_regime else 20.0,
                "pcr": float(nifty_chain.get("pcr", 1.0)),
                "max_pain": float(nifty_chain.get("max_pain", 0.0)),
                "net_gex": float(nifty_gex.get("net_gamma_crores", 0.0))
            },
            "sensex": {
                "ltp": sensex_p,
                "open": float(sensex_info.get("open", sensex_p)),
                "high": float(sensex_info.get("high", sensex_p)),
                "low": float(sensex_info.get("low", sensex_p)),
                "vwap": sensex_vwap,
                "change_pct": float(sensex_info.get("change_pct", 0.0)),
                "regime": sensex_regime.state.name if sensex_regime else "BALANCED_RANGE",
                "regime_score": sensex_regime.trend_score if sensex_regime else 50.0,
                "adx": sensex_regime.adx if sensex_regime else 20.0,
                "pcr": float(sensex_chain.get("pcr", 1.0)),
                "max_pain": float(sensex_chain.get("max_pain", 0.0)),
                "net_gex": float(sensex_gex.get("net_gamma_crores", 0.0))
            },
            "performance": {
                "active_count": len(active_running),
                "target_hits": target_hits,
                "stop_hits": stop_hits,
                "chase_blocked": chase_blocked,
                "theoretical_pnl": theoretical_pnl,
                "realized_pnl": realized_pnl,
                "circuit_breaker": getattr(rg, "circuit_breaker_tripped", False) if rg else False
            }
        }

    def format_digest_html(self, payload: Dict[str, Any], milestone_str: str = "") -> str:
        """Formats the digest payload into a scannable, mobile-optimized HTML report."""
        now_dt = payload["timestamp"]
        time_str = now_dt.strftime("%H:%M IST")
        date_str = now_dt.strftime("%d-%b-%Y")

        nf = payload["nifty"]
        sx = payload["sensex"]
        perf = payload["performance"]

        # VWAP differences
        nf_vwap_diff = nf["ltp"] - nf["vwap"]
        sx_vwap_diff = sx["ltp"] - sx["vwap"]

        # Badges
        def get_regime_badge(regime_name: str, score: float) -> str:
            if "BEAR" in regime_name:
                return f"🔴 <code>{regime_name}</code> ({score:.0f})"
            elif "BULL" in regime_name:
                return f"🟢 <code>{regime_name}</code> ({score:.0f})"
            else:
                return f"🟡 <code>{regime_name}</code> ({score:.0f})"

        nf_badge = get_regime_badge(nf["regime"], nf["regime_score"])
        sx_badge = get_regime_badge(sx["regime"], sx["regime_score"])

        pnl = perf["theoretical_pnl"]
        pnl_str = f"+₹{pnl:,.2f}" if pnl >= 0 else f"-₹{abs(pnl):,.2f}"
        pnl_icon = "🟢" if pnl >= 0 else "🔴"

        # Formulate tactical guidance
        guidance = self._generate_tactical_guidance(nf, sx, now_dt)

        ms_tag = f" | <i>Milestone {milestone_str}</i>" if milestone_str else ""

        return (
            f"📊 <b>ALPHA 3.0 — 45-MIN MARKET DIGEST</b>\n"
            f"🕒 <b>{time_str}</b> ({date_str}){ms_tag}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔹 <b>NIFTY 50</b>: <code>{nf['ltp']:.2f}</code> (<code>{nf['change_pct']:+.2f}%</code>)\n"
            f"• <b>Range:</b> {nf['low']:.1f} – {nf['high']:.1f} | <b>VWAP:</b> {nf['vwap']:.1f} (<code>{nf_vwap_diff:+.1f}</code>)\n"
            f"• <b>Regime:</b> {nf_badge} | <b>ADX:</b> <code>{nf['adx']:.1f}</code>\n"
            f"• <b>Chain:</b> PCR: <code>{nf['pcr']:.2f}</code> | MaxPain: <code>{nf['max_pain']:.0f}</code>\n\n"
            f"🔸 <b>SENSEX</b>: <code>{sx['ltp']:.2f}</code> (<code>{sx['change_pct']:+.2f}%</code>)\n"
            f"• <b>Range:</b> {sx['low']:.1f} – {sx['high']:.1f} | <b>VWAP:</b> {sx['vwap']:.1f} (<code>{sx_vwap_diff:+.1f}</code>)\n"
            f"• <b>Regime:</b> {sx_badge} | <b>ADX:</b> <code>{sx['adx']:.1f}</code>\n"
            f"• <b>Chain:</b> PCR: <code>{sx['pcr']:.2f}</code> | MaxPain: <code>{sx['max_pain']:.0f}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ <b>EXECUTION & SIGNALS TRACKER</b>\n"
            f"• <b>Active Running:</b> <code>{perf['active_count']} trade(s)</code>\n"
            f"• <b>Outcomes:</b> 🏆 <code>{perf['target_hits']} Wins</code> | 🛑 <code>{perf['stop_hits']} Stops</code> | 🚫 <code>{perf['chase_blocked']} Blocked</code>\n"
            f"• <b>Session PnL:</b> {pnl_icon} <b>{pnl_str}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🧭 <b>TACTICAL BIAS (NEXT 45 MINS):</b>\n"
            f"<i>{guidance}</i>"
        )

    def _generate_tactical_guidance(self, nf: Dict[str, Any], sx: Dict[str, Any], dt: datetime) -> str:
        """Synthesizes an actionable market bias based on current metrics and time."""
        is_expiry_day = (dt.weekday() in (1, 3, 4))
        is_late_expiry = is_expiry_day and (dt.hour > 14 or (dt.hour == 14 and dt.minute >= 15))

        if is_late_expiry:
            return "⏳ Expiry Time Gate Active (>14:15 IST). New naked option buys paused to prevent theta bleed. Focus on managing open runners."

        nf_bear = nf["ltp"] < nf["vwap"] and "BEAR" in nf["regime"]
        sx_bear = sx["ltp"] < sx["vwap"] and "BEAR" in sx["regime"]

        nf_bull = nf["ltp"] > nf["vwap"] and "BULL" in nf["regime"]
        sx_bull = sx["ltp"] > sx["vwap"] and "BULL" in sx["regime"]

        if nf_bear and sx_bear:
            return "Strong Downward Trend. Both indices below VWAP with high ADX. Favor PE pullback rejections into EMA9/21. Avoid CE counter-trend buys."
        elif nf_bull and sx_bull:
            return "Bullish Momentum. Both indices trading firmly above VWAP. Favor CE pullback entries. Suppress PE short attempts."
        elif nf["ltp"] < nf["vwap"] or sx["ltp"] < sx["vwap"]:
            return "Bearish Tilt / Distribution. Overhead VWAP resistance capping rallies. Wait for clear directional expansion before committing."
        else:
            return "Consolidation / Rangebound. Low ADX participation. Rely on strict confirmation wicks and protect capital from whipsaws."

    async def send_digest(self, milestone_str: str = "") -> bool:
        """Builds, formats, and dispatches the digest report via Telegram."""
        if not self.enabled:
            return False

        notifier = getattr(self.server, "telegram", None)
        if not notifier or not notifier.enabled:
            logger.info("Periodic reporter: Telegram notifier not active or disabled.")
            return False

        try:
            payload = self.build_digest_payload()
            html_msg = self.format_digest_html(payload, milestone_str=milestone_str)
            await notifier.enqueue_message(html_msg)
            logger.success(f"📢 [PERIODIC DIGEST] Dispatched 45-min Telegram analysis report ({milestone_str})!")
            return True
        except Exception as e:
            logger.error(f"Error generating/sending periodic digest: {e}")
            return False

    async def start(self):
        """Starts the background scheduler loop."""
        self.running = True
        self._task = asyncio.create_task(self._scheduler_loop())
        logger.info(f"📊 Periodic Analysis Reporter started (Cadence: every {self.interval_minutes}m from 09:15 IST).")

    async def stop(self):
        """Stops the background scheduler loop."""
        self.running = False
        if self._task:
            self._task.cancel()
        logger.info("Periodic Analysis Reporter stopped.")

    async def _scheduler_loop(self):
        """Polls clock every 10 seconds to check against schedule slots."""
        while self.running:
            try:
                now_ist = datetime.now(IST)
                today_str = now_ist.strftime("%Y-%m-%d")
                time_str = now_ist.strftime("%H:%M")

                # Skip weekends
                if now_ist.weekday() >= 5:
                    await asyncio.sleep(60)
                    continue

                for idx, slot in enumerate(self.schedule_times, 1):
                    slot_key = f"{today_str} {slot}"
                    if time_str == slot and slot_key not in self.dispatched_slots:
                        self.dispatched_slots.add(slot_key)
                        milestone_label = f"#{idx} ({slot} IST)"
                        logger.info(f"⏰ [PERIODIC REPORTER] Triggering scheduled digest for slot {slot_key}...")
                        await self.send_digest(milestone_str=milestone_label)

                # Clean up yesterday's slot keys
                self.dispatched_slots = {k for k in self.dispatched_slots if k.startswith(today_str)}

                await asyncio.sleep(10)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Periodic reporter scheduler loop error: {e}")
                await asyncio.sleep(15)
