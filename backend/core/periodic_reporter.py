import os
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from loguru import logger
import yaml

IST = timezone(timedelta(hours=5, minutes=30))

DEFAULT_SCHEDULE = [
    "09:30", "09:45", "10:00", "10:15", "10:30", "10:45",
    "11:00", "11:15", "11:30", "11:45", "12:00", "12:15",
    "12:30", "12:45", "13:00", "13:15", "13:30", "13:45",
    "14:00", "14:15", "14:30", "14:45", "15:00", "15:15"
]


class PeriodicAnalysisReporter:
    """
    Intraday Periodic Market & Signals Digest Reporter for Alpha 3.0.
    Executes every 30 minutes starting from 09:30 AM IST during market hours.
    Compiles:
      - Live Spot & Range Telemetry (NIFTY & SENSEX)
      - Session VWAP & EMA Alignment
      - Trend Regime & Market Direction
      - Gamma Exposure (GEX) & Wall Structure (Call Wall, Put Wall, Strength, Breach Status)
      - Option Chain Telemetry (PCR, MaxPain)
      - Cumulative Signal Tracker Performance & Net PnL
      - Tactical Actionable Playbook for the upcoming 30-minute window
    Dispatches rich HTML reports directly to Telegram.
    """

    def __init__(self, server: Any, config_path: Optional[str] = None):
        self.server = server
        self.enabled = True
        self.interval_minutes = 30
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
        nifty_vwap = regime_vwaps.get("NIFTY").value if regime_vwaps.get("NIFTY") else None
        sensex_vwap = regime_vwaps.get("SENSEX").value if regime_vwaps.get("SENSEX") else None

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

        target_hits = sum(1 for s in active_sigs if s.get("status") == "TARGET_HIT")
        stop_hits = sum(1 for s in active_sigs if s.get("status") == "STOP_HIT")
        chase_blocked = sum(1 for s in active_sigs if s.get("status") == "INVALID_CHASE_PREVENTED")
        theoretical_pnl = sum(float(s.get("theoretical_pnl", 0.0)) for s in active_sigs)

        # 6. GEX Engine
        gex = getattr(self.server, "gex_engine", None)
        nifty_gex = gex.get_current_metrics("NIFTY") if gex and hasattr(gex, "get_current_metrics") else {}
        sensex_gex = gex.get_current_metrics("SENSEX") if gex and hasattr(gex, "get_current_metrics") else {}

        # 7. Comprehensive Fallback to Local REST API & Execution Engine
        t_data: Dict[str, Any] = {}
        try:
            import json
            import urllib.request
            req = urllib.request.urlopen("http://localhost:8001/api/telemetry", timeout=1.5)
            t_data = json.loads(req.read().decode("utf-8"))
            
            # Gamma / GEX fallback
            gamma_data = t_data.get("gamma", {})
            if not nifty_gex.get("call_wall") and gamma_data.get("NIFTY"):
                nifty_gex = gamma_data["NIFTY"]
            if not sensex_gex.get("call_wall") and gamma_data.get("SENSEX"):
                sensex_gex = gamma_data["SENSEX"]
            
            # Spot data fallback
            sp_data = t_data.get("spot_data", {})
            if (not nifty_p or nifty_p <= 0) and sp_data.get("NIFTY"):
                nifty_info = sp_data["NIFTY"]
                nifty_p = float(nifty_info.get("ltp", 0.0))
            elif sp_data.get("NIFTY") and not nifty_info.get("open"):
                nifty_info = sp_data["NIFTY"]

            if (not sensex_p or sensex_p <= 0) and sp_data.get("SENSEX"):
                sensex_info = sp_data["SENSEX"]
                sensex_p = float(sensex_info.get("ltp", 0.0))
            elif sp_data.get("SENSEX") and not sensex_info.get("open"):
                sensex_info = sp_data["SENSEX"]
        except Exception as e:
            logger.debug(f"Telemetry API fallback skipped: {e}")

        # Regime fallback from telemetry
        t_regimes = t_data.get("regimes", {})
        n_reg = t_regimes.get("NIFTY", {})
        nifty_regime_name = nifty_regime.state.name if nifty_regime else n_reg.get("regime", "BALANCED_RANGE")
        nifty_regime_score = nifty_regime.trend_score if nifty_regime else float(n_reg.get("score", 50.0))
        nifty_adx = nifty_regime.adx if nifty_regime else float(n_reg.get("adx_value", 20.0))

        s_reg = t_regimes.get("SENSEX", {})
        sensex_regime_name = sensex_regime.state.name if sensex_regime else s_reg.get("regime", "BALANCED_RANGE")
        sensex_regime_score = sensex_regime.trend_score if sensex_regime else float(s_reg.get("score", 50.0))
        sensex_adx = sensex_regime.adx if sensex_regime else float(s_reg.get("adx_value", 20.0))

        # Robust VWAP fallback: compute typical price approximation if server object missing
        if not nifty_vwap or nifty_vwap <= 0:
            n_o = float(nifty_info.get("open", nifty_p))
            n_h = float(nifty_info.get("high", nifty_p))
            n_l = float(nifty_info.get("low", nifty_p))
            nifty_vwap = round((n_o + n_h + n_l + 2 * nifty_p) / 5, 2) if nifty_p > 0 else 0.0

        if not sensex_vwap or sensex_vwap <= 0:
            s_o = float(sensex_info.get("open", sensex_p))
            s_h = float(sensex_info.get("high", sensex_p))
            s_l = float(sensex_info.get("low", sensex_p))
            sensex_vwap = round((s_o + s_h + s_l + 2 * sensex_p) / 5, 2) if sensex_p > 0 else 0.0

        # Chain metrics (PCR & MaxPain) fallback from GEX if chain_poller missing
        nifty_pcr = float(nifty_chain.get("pcr", 0.0))
        if nifty_pcr <= 0 and nifty_gex.get("call_wall_oi"):
            nifty_pcr = round(float(nifty_gex.get("put_wall_oi", 1)) / float(nifty_gex["call_wall_oi"]), 2)
        nifty_max_pain = float(nifty_chain.get("max_pain", 0.0)) or float(nifty_gex.get("put_wall", 0.0))

        sensex_pcr = float(sensex_chain.get("pcr", 0.0))
        if sensex_pcr <= 0 and sensex_gex.get("call_wall_oi"):
            sensex_pcr = round(float(sensex_gex.get("put_wall_oi", 1)) / float(sensex_gex["call_wall_oi"]), 2)
        sensex_max_pain = float(sensex_chain.get("max_pain", 0.0)) or float(sensex_gex.get("put_wall", 0.0))

        # Execution tracking fallback: fetch from execution API or today's orders file
        active_exec_count = len(active_running)
        try:
            import os, json, urllib.request
            req_exec = urllib.request.urlopen("http://localhost:8001/api/execution/status", timeout=1.0)
            e_status = json.loads(req_exec.read().decode("utf-8"))
            active_exec_count = e_status.get("active_positions_count", active_exec_count)

            today_orders_file = f"data/execution_logs/orders_{today_str}.json"
            if os.path.exists(today_orders_file):
                with open(today_orders_file, "r", encoding="utf-8") as f:
                    orders_log = json.load(f)
                file_pnl = 0.0
                file_wins = 0
                file_stops = 0
                for o in orders_log:
                    if o.get("action") == "FULL_EXIT":
                        pnl_val = float(o.get("total_realized_pnl", 0.0))
                        file_pnl += pnl_val
                        if pnl_val > 0:
                            file_wins += 1
                        elif pnl_val < 0:
                            file_stops += 1
                    elif o.get("target_1_hit") and float(o.get("realized_pnl", 0.0)) > 0:
                        file_pnl += float(o.get("realized_pnl", 0.0))
                        file_wins += 1
                if file_wins > 0 or file_stops > 0:
                    realized_pnl = file_pnl
                    target_hits = file_wins
                    stop_hits = file_stops
                    theoretical_pnl = file_pnl
        except Exception:
            pass

        return {
            "timestamp": now_dt,
            "interval_minutes": self.interval_minutes,
            "nifty": {
                "ltp": nifty_p,
                "open": float(nifty_info.get("open", nifty_p)),
                "high": float(nifty_info.get("high", nifty_p)),
                "low": float(nifty_info.get("low", nifty_p)),
                "vwap": nifty_vwap,
                "change_pct": float(nifty_info.get("change_pct", 0.0)),
                "regime": nifty_regime_name,
                "regime_score": nifty_regime_score,
                "adx": nifty_adx,
                "pcr": nifty_pcr,
                "max_pain": nifty_max_pain,
                "net_gex": float(nifty_gex.get("net_gex", 0.0)),
                "call_wall": float(nifty_gex.get("call_wall", 0.0)),
                "call_wall_oi": int(nifty_gex.get("call_wall_oi", 0)),
                "call_wall_gex": float(nifty_gex.get("call_wall_gex", 0.0)),
                "call_wall_dist": float(nifty_gex.get("call_wall_dist", 0.0)),
                "call_wall_status": str(nifty_gex.get("call_wall_status", "ACTIVE_CEILING")),
                "call_wall_strength": str(nifty_gex.get("call_wall_strength", "MODERATE")),
                "put_wall": float(nifty_gex.get("put_wall", 0.0)),
                "put_wall_oi": int(nifty_gex.get("put_wall_oi", 0)),
                "put_wall_gex": float(nifty_gex.get("put_wall_gex", 0.0)),
                "put_wall_dist": float(nifty_gex.get("put_wall_dist", 0.0)),
                "put_wall_status": str(nifty_gex.get("put_wall_status", "ACTIVE_FLOOR")),
                "put_wall_strength": str(nifty_gex.get("put_wall_strength", "MODERATE")),
                "gamma_regime": str(nifty_gex.get("regime_detail", "NEUTRAL"))
            },
            "sensex": {
                "ltp": sensex_p,
                "open": float(sensex_info.get("open", sensex_p)),
                "high": float(sensex_info.get("high", sensex_p)),
                "low": float(sensex_info.get("low", sensex_p)),
                "vwap": sensex_vwap,
                "change_pct": float(sensex_info.get("change_pct", 0.0)),
                "regime": sensex_regime_name,
                "regime_score": sensex_regime_score,
                "adx": sensex_adx,
                "pcr": sensex_pcr,
                "max_pain": sensex_max_pain,
                "net_gex": float(sensex_gex.get("net_gex", 0.0)),
                "call_wall": float(sensex_gex.get("call_wall", 0.0)),
                "call_wall_oi": int(sensex_gex.get("call_wall_oi", 0)),
                "call_wall_gex": float(sensex_gex.get("call_wall_gex", 0.0)),
                "call_wall_dist": float(sensex_gex.get("call_wall_dist", 0.0)),
                "call_wall_status": str(sensex_gex.get("call_wall_status", "ACTIVE_CEILING")),
                "call_wall_strength": str(sensex_gex.get("call_wall_strength", "MODERATE")),
                "put_wall": float(sensex_gex.get("put_wall", 0.0)),
                "put_wall_oi": int(sensex_gex.get("put_wall_oi", 0)),
                "put_wall_gex": float(sensex_gex.get("put_wall_gex", 0.0)),
                "put_wall_dist": float(sensex_gex.get("put_wall_dist", 0.0)),
                "put_wall_status": str(sensex_gex.get("put_wall_status", "ACTIVE_FLOOR")),
                "put_wall_strength": str(sensex_gex.get("put_wall_strength", "MODERATE")),
                "gamma_regime": str(sensex_gex.get("regime_detail", "NEUTRAL"))
            },
            "performance": {
                "active_count": active_exec_count,
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
        interval = payload.get("interval_minutes", 30)

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

        # Format GEX walls helper
        def format_gex_lines(item: Dict[str, Any]) -> str:
            cw = item.get("call_wall", 0.0)
            pw = item.get("put_wall", 0.0)
            raw_net_gex = item.get("net_gex", 0.0)
            net_gex = (raw_net_gex / 1e7) if abs(raw_net_gex) > 1e4 else raw_net_gex

            gex_regime_badge = "⚡ <i>Amplifier</i>" if net_gex < -5.0 else ("🛡️ <i>Dampener</i>" if net_gex > 5.0 else "⚖️ <i>Neutral</i>")

            if cw <= 0 and pw <= 0:
                return f"• <b>Net GEX:</b> <code>{net_gex:+.1f} Cr</code> {gex_regime_badge}\n"

            # Call Wall status
            cw_stat = item.get("call_wall_status", "ACTIVE_CEILING")
            if cw_stat == "BREACHED_UP":
                cw_tag = "🚀 <b>SQUEEZE ACTIVE</b>"
            elif cw_stat == "TESTING_CEILING":
                cw_tag = "⚠️ <b>UNDER SIEGE</b>"
            elif item.get("call_wall_strength") == "FORTIFIED":
                cw_tag = "🛡️ <b>FORTIFIED CEILING</b>"
            else:
                cw_tag = "🧱 <b>RESISTANCE</b>"

            cw_oi = item.get("call_wall_oi", 0)
            cw_oi_str = f"{cw_oi / 1e6:.1f}M OI" if cw_oi >= 1e6 else (f"{cw_oi / 1e3:.0f}k OI" if cw_oi > 0 else "")
            raw_cw_gex = item.get("call_wall_gex", 0.0)
            cw_gex = (raw_cw_gex / 1e7) if abs(raw_cw_gex) > 1e4 else raw_cw_gex
            cw_extra = f" [{cw_oi_str} | +{cw_gex:.1f} Cr]" if cw_oi_str else ""
            cw_dist = item.get("call_wall_dist", 0.0)
            if cw_dist < 0:
                cw_dist_str = f"+{abs(cw_dist):.0f} pts pierced"
            else:
                cw_dist_str = f"{cw_dist:.0f} pts away"

            # Put Wall status & Tag
            pw_stat = item.get("put_wall_status", "ACTIVE_FLOOR")
            pw_dist = item.get("put_wall_dist", 0.0)
            if pw_stat == "BREACHED_DOWN" or pw_dist < 0:
                pw_tag = "🚨 <b>CASCADE ACTIVE (CRACKED)</b>"
            elif pw_stat == "TESTING_FLOOR":
                pw_tag = "🛡️ <b>TESTING SUPPORT FLOOR</b>"
            elif item.get("put_wall_strength") == "FORTIFIED":
                pw_tag = "🛡️ <b>FORTIFIED FLOOR</b>"
            else:
                pw_tag = "🧱 <b>SUPPORT</b>"

            pw_oi = item.get("put_wall_oi", 0)
            pw_oi_str = f"{pw_oi / 1e6:.1f}M OI" if pw_oi >= 1e6 else (f"{pw_oi / 1e3:.0f}k OI" if pw_oi > 0 else "")
            raw_pw_gex = item.get("put_wall_gex", 0.0)
            pw_gex = (raw_pw_gex / 1e7) if abs(raw_pw_gex) > 1e4 else raw_pw_gex
            pw_extra = f" [{pw_oi_str} | -{pw_gex:.1f} Cr]" if pw_oi_str else ""
            
            if pw_dist > 0:
                pw_dist_str = f"+{pw_dist:.0f} pts cushion"
            elif pw_dist < 0:
                pw_dist_str = f"-{abs(pw_dist):.0f} pts cracked"
            else:
                pw_dist_str = "at floor"

            return (
                f"• <b>Net GEX:</b> <code>{net_gex:+.1f} Cr</code> {gex_regime_badge}\n"
                f"• <b>Call Wall:</b> <code>{cw:.0f} CE</code> ({cw_dist_str}) | {cw_tag}{cw_extra}\n"
                f"• <b>Put Wall:</b> <code>{pw:.0f} PE</code> ({pw_dist_str}) | {pw_tag}{pw_extra}\n"
            )

        nf_gex_block = format_gex_lines(nf)
        sx_gex_block = format_gex_lines(sx)

        pnl = perf["theoretical_pnl"]
        pnl_str = f"+₹{pnl:,.2f}" if pnl >= 0 else f"-₹{abs(pnl):,.2f}"
        pnl_icon = "🟢" if pnl >= 0 else "🔴"

        # Formulate Senior CMT / Quantitative guidance and probabilities
        cm_data = self._compute_cmt_analytics(nf, sx, now_dt)
        guidance = cm_data["tactical_playbook"]

        ms_tag = f" | <i>Milestone {milestone_str}</i>" if milestone_str else ""

        return (
            f"🏛️ <b>ALPHA 3.0 — {interval}-MIN MARKET INTELLIGENCE</b>\n"
            f"🕒 <b>{time_str}</b> ({date_str}){ms_tag}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎲 <b>PROBABILITY MATRIX (NEXT {interval}M):</b>\n"
            f"• 🟢 <b>Bullish Expansion:</b> <code>{cm_data['p_bull']:.1f}%</code>\n"
            f"• 🟡 <b>Range / Pinning:</b> <code>{cm_data['p_range']:.1f}%</code>\n"
            f"• 🔴 <b>Breakdown Reversal:</b> <code>{cm_data['p_bear']:.1f}%</code>\n"
            f"• <b>CMT Phase:</b> <code>{cm_data['market_phase']}</code> | <b>POC:</b> <code>{cm_data['poc']:.1f}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔹 <b>NIFTY 50</b>: <code>{nf['ltp']:.2f}</code> (<code>{nf['change_pct']:+.2f}%</code>)\n"
            f"• <b>Range:</b> {nf['low']:.1f} – {nf['high']:.1f} | <b>VWAP:</b> {nf['vwap']:.1f} (<code>{nf_vwap_diff:+.1f}</code>)\n"
            f"• <b>Regime:</b> {nf_badge} | <b>ADX:</b> <code>{nf['adx']:.1f}</code>\n"
            f"• <b>Chain:</b> PCR: <code>{nf['pcr']:.2f}</code> | MaxPain: <code>{nf['max_pain']:.0f}</code>\n"
            f"{nf_gex_block}\n"
            f"🔸 <b>SENSEX</b>: <code>{sx['ltp']:.2f}</code> (<code>{sx['change_pct']:+.2f}%</code>)\n"
            f"• <b>Range:</b> {sx['low']:.1f} – {sx['high']:.1f} | <b>VWAP:</b> {sx['vwap']:.1f} (<code>{sx_vwap_diff:+.1f}</code>)\n"
            f"• <b>Regime:</b> {sx_badge} | <b>ADX:</b> <code>{sx['adx']:.1f}</code>\n"
            f"• <b>Chain:</b> PCR: <code>{sx['pcr']:.2f}</code> | MaxPain: <code>{sx['max_pain']:.0f}</code>\n"
            f"{sx_gex_block}"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🛡️ <b>LIQUIDITY & TRAP RADAR:</b>\n"
            f"• {cm_data['liquidity_sweep_status']}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🧭 <b>ANTI-FOMO RE-ENTRY & TRADER PROTOCOL:</b>\n"
            f"• <b>Re-Entry Rule:</b> {cm_data['re_entry_protocol']}\n"
            f"• <b>Greek Shield:</b> {cm_data['greeks_advisory']}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ <b>EXECUTION & SIGNALS TRACKER</b>\n"
            f"• <b>Active Running:</b> <code>{perf['active_count']} trade(s)</code>\n"
            f"• <b>Outcomes:</b> 🏆 <code>{perf['target_hits']} Wins</code> | 🛑 <code>{perf['stop_hits']} Stops</code> | 🚫 <code>{perf['chase_blocked']} Blocked</code>\n"
            f"• <b>Session PnL:</b> {pnl_icon} <b>{pnl_str}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 <b>TACTICAL PLAYBOOK:</b>\n"
            f"<i>{guidance}</i>"
        )

    def _compute_cmt_analytics(self, nf: Dict[str, Any], sx: Dict[str, Any], dt: datetime) -> Dict[str, Any]:
        """Computes senior quantitative probability, CMT market phase, and liquidity sweep detection."""
        nf_p = nf.get("ltp", 0.0)
        nf_vwap = nf.get("vwap", nf_p)
        nf_high = nf.get("high", nf_p)
        nf_low = nf.get("low", nf_p)
        nf_adx = nf.get("adx", 20.0)
        nf_chg = nf.get("change_pct", 0.0)
        call_wall = nf.get("call_wall", 0.0)
        put_wall = nf.get("put_wall", 0.0)
        net_gex = nf.get("net_gex", 0.0)

        # 1. Quantitative Probability Calculation
        score = (nf_chg * 25.0) + (25.0 if nf_p > nf_vwap else -25.0)
        if score >= 20.0:
            p_bull = min(85.0, 52.0 + (score * 0.35))
            if nf_adx >= 25:
                p_bull += min(8.0, (nf_adx - 25) * 0.4)
            if net_gex < 0:
                p_bull += 3.0  # Negative gamma accelerates moves
            p_bull = round(min(85.0, max(52.0, p_bull)), 1)
            p_range = round(max(10.0, min(35.0, (100.0 - p_bull) * 0.65)), 1)
            p_bear = round(100.0 - p_bull - p_range, 1)
        elif score <= -20.0:
            p_bear = min(85.0, 52.0 + (abs(score) * 0.35))
            if nf_adx >= 25:
                p_bear += min(8.0, (nf_adx - 25) * 0.4)
            if net_gex < 0:
                p_bear += 3.0
            p_bear = round(min(85.0, max(52.0, p_bear)), 1)
            p_range = round(max(10.0, min(35.0, (100.0 - p_bear) * 0.65)), 1)
            p_bull = round(100.0 - p_bear - p_range, 1)
        else:
            p_range = round(min(55.0, 42.0 + max(0.0, (25.0 - nf_adx) * 0.5)), 1)
            rem = 100.0 - p_range
            p_bull = round(rem * 0.55, 1) if score >= 0 else round(rem * 0.45, 1)
            p_bear = round(100.0 - p_bull - p_range, 1)

        # 2. CMT Market Phase
        if nf_p > nf_vwap and nf_adx >= 25:
            market_phase = "MARKUP / INSTITUTIONAL EXPANSION"
        elif nf_p < nf_vwap and nf_adx >= 25:
            market_phase = "MARKDOWN / DISTRIBUTION EXPANSION"
        elif nf_adx < 22:
            market_phase = "ACCUMULATION / RANGE COMPRESSION"
        else:
            market_phase = "CONSOLIDATION / RE-ACCUMULATION"

        # 3. Liquidity Sweep vs Breakdown Radar
        if put_wall > 0 and nf_low < put_wall and nf_p > put_wall:
            liquidity_sweep_status = (
                f"BEAR TRAP / LIQUIDITY SWEEP VERIFIED: Dip to {nf_low:.1f} wicked below Put Wall ({put_wall:.0f}), "
                f"but closed with zero 5-minute candle bodies below. Absorbed into institutional markup."
            )
        elif call_wall > 0 and nf_high > call_wall and nf_p < call_wall:
            liquidity_sweep_status = (
                f"BULL TRAP / RESISTANCE REJECTION: Spike to {nf_high:.1f} tested Call Wall ({call_wall:.0f}) "
                f"without sustained 5-min close above. Profit booking active."
            )
        else:
            liquidity_sweep_status = (
                f"Order flow clean. Put Wall ({put_wall:.0f}) and Call Wall ({call_wall:.0f}) "
                f"maintaining clear structural boundaries."
            )

        # 4. Anti-FOMO Re-Entry Blueprint
        poc = nf_vwap
        target_strike = (call_wall + 50.0) if (call_wall > 0 and nf_p >= call_wall) else call_wall
        if p_bull >= 50.0:
            re_entry_protocol = (
                f"If shaken out on wicks: Confirmed re-entry trigger is a 5-min candle close reclaiming "
                f"VWAP ({poc:.0f}) or clearing {round(nf_p + 15, 0)}. Target: {target_strike:.0f}. "
                f"Invalidation: 5-min body close below Put Wall ({put_wall:.0f})."
            )
        elif p_bear >= 50.0:
            re_entry_protocol = (
                f"If shaken out on wicks: Confirmed re-entry trigger is a 5-min candle close below "
                f"VWAP ({poc:.0f}). Target: {put_wall:.0f}. "
                f"Invalidation: 5-min body close above Call Wall ({call_wall:.0f})."
            )
        else:
            re_entry_protocol = "Rangebound protocol: Fade boundaries only (Sell Call Wall, Buy Put Wall with tight stops)."

        # 5. 0-DTE Greek Advisory
        greeks_advisory = (
            "0-DTE Gamma Advisory: Premiums fluctuate ±15–25% on normal pullbacks. "
            "Anchor stops strictly on Spot/VWAP levels, never off option ticker panic."
        )

        tactical_playbook = self._generate_tactical_guidance(nf, sx, dt)

        return {
            "p_bull": p_bull,
            "p_range": p_range,
            "p_bear": p_bear,
            "market_phase": market_phase,
            "poc": poc,
            "liquidity_sweep_status": liquidity_sweep_status,
            "re_entry_protocol": re_entry_protocol,
            "greeks_advisory": greeks_advisory,
            "tactical_playbook": tactical_playbook
        }

    def _generate_tactical_guidance(self, nf: Dict[str, Any], sx: Dict[str, Any], dt: datetime) -> str:
        """Synthesizes an actionable market bias based on current metrics and time."""
        is_expiry_day = (dt.weekday() in (1, 3, 4))
        is_late_expiry = is_expiry_day and (dt.hour > 14 or (dt.hour == 14 and dt.minute >= 15))

        if is_late_expiry:
            return "⏳ Expiry Time Gate Active (>14:15 IST). New naked option buys paused to prevent theta bleed. Focus on managing open runners."

        # Check for active gamma breaches
        if sx.get("put_wall_status") == "BREACHED_DOWN" or nf.get("put_wall_status") == "BREACHED_DOWN":
            return "🚨 Short Gamma Cascade Active: Spot broken below Put Wall! Put writers capitulating forces dealer short delta dumping. Downside moves amplify rapidly. Prioritize PE on EMA pullbacks; strictly avoid dip-buying CE."

        if sx.get("call_wall_status") == "BREACHED_UP" or nf.get("call_wall_status") == "BREACHED_UP":
            return "🚀 Short Squeeze Breakout Active: Spot pierced above Call Wall! Trapped call writers scrambling to cover. Upward momentum accelerating. Favor CE pullback entries; avoid PE short attempts."

        nf_bear = nf["ltp"] < nf["vwap"] and "BEAR" in nf["regime"]
        sx_bear = sx["ltp"] < sx["vwap"] and "BEAR" in sx["regime"]

        nf_bull = nf["ltp"] > nf["vwap"] and "BULL" in nf["regime"]
        sx_bull = sx["ltp"] > sx["vwap"] and "BULL" in sx["regime"]

        if nf_bear and sx_bear:
            return "Strong Bear Trend. Both indices below VWAP with high ADX. Overhead Call Wall acts as fortified ceiling. Favor PE pullback rejections into EMA9/21. Avoid CE counter-trend buys."
        elif nf_bull and sx_bull:
            return "Bullish Momentum. Both indices trading firmly above VWAP supported by Put Wall floor. Favor CE pullback entries. Suppress PE counter-trend shorts."
        elif nf["ltp"] < nf["vwap"] or sx["ltp"] < sx["vwap"]:
            return "Bearish Tilt / Distribution. Overhead VWAP & Call Wall capping rallies. Wait for clear directional expansion before committing."
        else:
            return "Consolidation / Corridor Rangebound. Spot trapped between Put Wall floor and Call Wall ceiling. Low ADX participation. Rely on strict confirmation wicks and protect capital from whipsaws."

    async def send_digest(self, milestone_str: str = "") -> bool:
        """Builds, formats, and dispatches the digest report via Telegram."""
        if not self.enabled:
            return False

        notifier = getattr(self.server, "telegram", None)
        local_notifier = False
        if not notifier or not getattr(notifier, "enabled", False):
            from backend.core.telegram_notifier import TelegramNotifier
            notifier = TelegramNotifier()
            await notifier.initialize()
            local_notifier = True

        if not notifier.enabled or not getattr(notifier, "client", None):
            logger.warning("Periodic reporter: Telegram notifier not active or disabled.")
            return False

        try:
            payload = self.build_digest_payload()
            html_msg = self.format_digest_html(payload, milestone_str=milestone_str)
            await notifier.enqueue_message(html_msg)
            if local_notifier:
                await asyncio.sleep(2.5)
                await notifier.close()
            logger.success(f"📢 [PERIODIC DIGEST] Dispatched 15-min Telegram analysis report ({milestone_str})!")
            return True
        except Exception as e:
            logger.error(f"Error generating/sending periodic digest: {e}")
            if local_notifier:
                try:
                    await notifier.close()
                except Exception:
                    pass
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
