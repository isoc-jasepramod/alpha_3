import os
import asyncio
import httpx
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from loguru import logger
import yaml

IST = timezone(timedelta(hours=5, minutes=30))

class TelegramNotifier:
    """
    Asynchronous Telegram Notification Dispatcher for Alpha 3.0.
    Consumes signal and radar alert events and dispatches rich HTML messages
    via Telegram Bot API with rate-limiting, retries, and non-blocking background queue.
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        enabled: Optional[bool] = None,
        config_path: Optional[str] = None
    ):
        # 1. Read from environment variables (.env) or kwargs
        self.bot_token = (bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip().strip('"').strip("'")
        self.raw_chat_id = (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip().strip('"').strip("'")
        
        env_enabled = os.getenv("TELEGRAM_ENABLED", "").lower() in ("true", "1", "yes")
        self.enabled = enabled if enabled is not None else (env_enabled or bool(self.bot_token))

        # 2. Settings configuration
        cfg_path = config_path or os.path.join(os.path.dirname(__file__), "..", "..", "config", "settings.yaml")
        self.parse_mode = "HTML"
        self.rate_limit_per_sec = 1.0
        self.notify_signals = True
        self.notify_radar = True
        self.notify_resolutions = True

        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, "r") as f:
                    t_cfg = yaml.safe_load(f).get("telegram", {})
                    if "enabled" in t_cfg and enabled is None and not env_enabled:
                        self.enabled = bool(t_cfg.get("enabled"))
                    self.parse_mode = t_cfg.get("parse_mode", self.parse_mode)
                    self.rate_limit_per_sec = float(t_cfg.get("rate_limit_per_sec", 1.0))
                    n_cfg = t_cfg.get("notifications", {})
                    self.notify_signals = bool(n_cfg.get("signals", True))
                    self.notify_radar = bool(n_cfg.get("radar_precursors", True))
                    self.notify_resolutions = bool(n_cfg.get("resolutions", True))
            except Exception as e:
                logger.warning(f"Error reading telegram settings: {e}")

        self.raw_targets = [x.strip() for x in self.raw_chat_id.split(",") if x.strip()]
        self.resolved_chat_ids: List[str] = []
        self.bot_username: Optional[str] = None
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.client: Optional[httpx.AsyncClient] = None
        self._worker_task: Optional[asyncio.Task] = None
        self.running = False

    @property
    def resolved_chat_id(self) -> Optional[str]:
        """Convenience property for single chat or first resolved ID."""
        return self.resolved_chat_ids[0] if self.resolved_chat_ids else None

    @resolved_chat_id.setter
    def resolved_chat_id(self, val: Optional[str]):
        if val:
            if val not in self.resolved_chat_ids:
                self.resolved_chat_ids.append(val)
        else:
            self.resolved_chat_ids.clear()

    @property
    def api_url(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"

    async def initialize(self):
        """Verifies bot credentials and initializes async HTTP client and worker."""
        if not self.enabled or not self.bot_token:
            logger.info("Telegram notifications disabled or BOT_TOKEN not configured.")
            return

        self.client = httpx.AsyncClient(timeout=10.0)
        try:
            res = await self.client.get(f"{self.api_url}/getMe")
            data = res.json()
            if data.get("ok"):
                self.bot_username = data.get("result", {}).get("username", "")
                logger.success(f"📱 Connected to Telegram Bot @{self.bot_username}!")
                await self._resolve_chat_ids()
            else:
                logger.error(f"Telegram getMe failed: {data}")
        except Exception as e:
            logger.error(f"Failed to connect to Telegram Bot API: {e}")

        self.running = True
        self._worker_task = asyncio.create_task(self._dispatcher_loop())

    async def close(self):
        self.running = False
        if self._worker_task:
            self._worker_task.cancel()
        if self.client:
            await self.client.aclose()
            self.client = None

    async def _resolve_chat_ids(self) -> List[str]:
        """
        Resolves numeric chat_ids for all configured targets.
        Supports comma-separated IDs, channels (-100...), and usernames.
        """
        updates = None
        for raw in self.raw_targets:
            clean = raw.lstrip("@")
            # If numeric or group/channel handle
            if clean.isdigit() or clean.startswith("-"):
                if raw not in self.resolved_chat_ids:
                    self.resolved_chat_ids.append(raw)
                continue

            # Query getUpdates if not fetched yet
            if updates is None:
                try:
                    res = await self.client.get(f"{self.api_url}/getUpdates")
                    updates = res.json().get("result", [])
                except Exception as e:
                    logger.warning(f"Could not fetch Telegram getUpdates: {e}")
                    updates = []

            matched = False
            for upd in updates:
                msg = upd.get("message") or upd.get("channel_post") or {}
                chat = msg.get("chat", {})
                username = chat.get("username", "")
                cid = str(chat.get("id"))
                if username.lower() == clean.lower():
                    if cid not in self.resolved_chat_ids:
                        self.resolved_chat_ids.append(cid)
                    logger.success(f"Resolved Telegram target @{username} to Chat ID: {cid}")
                    matched = True
                    break

            if not matched and updates and not self.resolved_chat_ids:
                # Fallback to latest chat if nothing resolved yet
                latest_chat = updates[-1].get("message", {}).get("chat", {})
                if latest_chat.get("id"):
                    cid = str(latest_chat.get("id"))
                    if cid not in self.resolved_chat_ids:
                        self.resolved_chat_ids.append(cid)
                    u = latest_chat.get("username", "user")
                    logger.info(f"Auto-bound to latest Telegram chat: @{u} (ID: {cid})")

            if not matched and clean not in [x.lstrip("@") for x in self.resolved_chat_ids]:
                logger.warning(
                    f"⚠️ Telegram target '{raw}' not yet resolved. "
                    f"Recipient must open https://t.me/{self.bot_username} and send /start"
                )

        return self.resolved_chat_ids

    async def _dispatcher_loop(self):
        """Worker loop that dequeues messages and posts them to all resolved Telegram chats."""
        while self.running:
            try:
                base_payload = await self.queue.get()
                if not self.resolved_chat_ids:
                    await self._resolve_chat_ids()

                if not self.resolved_chat_ids:
                    self.queue.task_done()
                    await asyncio.sleep(1.0)
                    continue

                # Broadcast to every recipient
                for chat_id in list(self.resolved_chat_ids):
                    payload = dict(base_payload)
                    payload["chat_id"] = chat_id
                    if "parse_mode" not in payload:
                        payload["parse_mode"] = self.parse_mode

                    try:
                        res = await self.client.post(f"{self.api_url}/sendMessage", json=payload)
                        resp_data = res.json()
                        if not resp_data.get("ok"):
                            logger.warning(f"Telegram sendMessage to {chat_id} error: {resp_data.get('description')}")
                            if resp_data.get("error_code") == 429:
                                retry_after = resp_data.get("parameters", {}).get("retry_after", 3)
                                await asyncio.sleep(retry_after)
                    except Exception as e:
                        logger.error(f"Error posting message to Telegram chat {chat_id}: {e}")

                self.queue.task_done()
                # Rate limit throttle between broadcasts
                await asyncio.sleep(1.0 / max(0.5, self.rate_limit_per_sec))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Telegram dispatcher error: {e}")
                await asyncio.sleep(1.0)

    async def enqueue_message(self, text: str):
        """Places text message onto queue."""
        if not self.enabled or not self.bot_token:
            return
        try:
            self.queue.put_nowait({"text": text})
        except asyncio.QueueFull:
            logger.warning("Telegram alert queue is full, dropping message.")

    def format_signal_html(self, sig: Dict[str, Any]) -> str:
        """Formats a NEW_SIGNAL event into a high-visibility Telegram HTML message."""
        inst = sig.get("instrument", "NIFTY")
        opt_type = sig.get("option_type", "CE")
        strike = sig.get("strike", 0.0)
        symbol = sig.get("option_symbol", f"{inst}{int(strike)}{opt_type}")
        strategy = sig.get("strategy", "UNKNOWN")
        direction = sig.get("direction", opt_type)
        entry = float(sig.get("entry_price", 0.0))
        sl = float(sig.get("stop_loss", 0.0))
        tgt = float(sig.get("target", 0.0))
        qty = int(sig.get("quantity", 0))
        lot_size = int(sig.get("lot_size", 50))
        lots = max(1, qty // lot_size) if lot_size > 0 else 1
        conf = sig.get("confidence", 100)
        risk = float(sig.get("risk_amount", 1000.0))

        details = sig.get("details", {})
        sqz_type = details.get("squeeze_type")
        strat_display = f"{strategy} ({sqz_type})" if sqz_type else strategy

        now_str = datetime.now(IST).strftime("%H:%M:%S IST")

        # Directional badge
        dir_badge = "🟢 <b>BUY CE (BULLISH)</b>" if opt_type == "CE" else "🔴 <b>BUY PE (BEARISH)</b>"

        # Risk-to-Reward calculation
        risk_pts = abs(entry - sl)
        reward_pts = abs(tgt - entry)
        rr_str = f"1:{reward_pts / risk_pts:.1f}" if risk_pts > 0 else "1:2.0"

        return (
            f"🚀 <b>ALPHA 3.0 — NEW SIGNAL</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Instrument:</b> <code>{inst}</code> | <b>Strike:</b> <code>{int(strike)} {opt_type}</code>\n"
            f"<b>Symbol:</b> <code>{symbol}</code>\n"
            f"<b>Strategy:</b> <code>{strat_display}</code>\n"
            f"<b>Action:</b> {dir_badge}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>Entry Price:</b> <code>₹{entry:.2f}</code>\n"
            f"🛑 <b>Stop Loss:</b> <code>₹{sl:.2f}</code> (-{risk_pts:.1f} pts)\n"
            f"🏁 <b>Target (+1.0R):</b> <code>₹{entry + (risk_pts if opt_type=='CE' else -risk_pts):.2f}</code> (Book 50%, Trail BE)\n"
            f"🏆 <b>Target (+2.0R):</b> <code>₹{tgt:.2f}</code> (+{reward_pts:.1f} pts)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Position:</b> <code>{lots} Lot(s) ({qty} Qty)</code>\n"
            f"💰 <b>Max Risk:</b> <code>₹{risk:.0f}</code> | <b>R/R:</b> <code>{rr_str}</code>\n"
            f"⚡ <b>Confidence:</b> <code>{conf}%</code>\n"
            f"🕒 <i>{now_str}</i>"
        )

    def format_radar_html(self, alert: Dict[str, Any]) -> str:
        """Formats a RADAR_PRE_ALERT (e.g. CONFLUENCE_PRE_ENTRY) into clean HTML."""
        alert_type = alert.get("alert_type", "PRE_ALERT")
        inst = alert.get("instrument", "NIFTY")
        direction = alert.get("direction", "CE")
        title = alert.get("title", "")
        message = alert.get("message", "")
        now_str = datetime.now(IST).strftime("%H:%M:%S IST")
        details = alert.get("details", {})

        is_confluence = "CONFLUENCE" in alert_type
        is_gamma_watch = "GAMMA_WATCH" in alert_type

        if is_gamma_watch:
            header = "⚡ <b>ELEVATED RADAR: GAMMA SCALP IMMINENT (0-DTE)</b>"
            bias_badge = "🟢 <b>CE BREAKOUT IMMINENT</b>" if direction == "CE" else "🔴 <b>PE BREAKDOWN IMMINENT</b>"
        elif is_confluence:
            header = "🎯 <b>ELEVATED RADAR: CONFLUENCE SETUP</b>"
            bias_badge = f"<b>{direction}</b>"
        else:
            header = f"📡 <b>RADAR PRE-ALERT: {alert_type}</b>"
            bias_badge = f"<b>{direction}</b>"

        plan_str = ""
        if is_confluence and details:
            entry_strike = details.get("entry_strike", 0.0)
            sl = details.get("recommended_sl", 0.0)
            t1 = details.get("target_1", 0.0)
            t2 = details.get("target_2", 0.0)
            plan_str = (
                f"\n📋 <b>Pre-Calculated Action Plan:</b>\n"
                f"• <b>Focus Strike:</b> <code>{int(entry_strike)} {direction}</code>\n"
                f"• <b>Spot SL:</b> <code>{sl:.1f}</code>\n"
                f"• <b>Projected T1:</b> <code>{t1:.0f}</code>\n"
                f"• <b>Projected T2:</b> <code>{t2:.0f}</code>\n"
            )

        return (
            f"{header}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Instrument:</b> <code>{inst}</code> | <b>Bias:</b> {bias_badge}\n"
            f"<b>Setup:</b> {title}\n"
            f"{plan_str}"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"ℹ️ {message}\n"
            f"🕒 <i>{now_str}</i>"
        )

    def format_resolution_html(self, sig: Dict[str, Any]) -> str:
        """Formats a SIGNAL_RESOLVED event (Target Hit, Stop Hit, Chase Prevented)."""
        status = sig.get("status", "")
        inst = sig.get("instrument", "NIFTY")
        symbol = sig.get("option_symbol", "")
        strategy = sig.get("strategy", "")
        entry = float(sig.get("entry_price", 0.0))
        exit_p = float(sig.get("exit_price", entry))
        pnl = float(sig.get("theoretical_pnl", 0.0))
        elapsed = sig.get("elapsed_sec", 0)
        dur_str = f"{elapsed // 60}m {elapsed % 60}s" if elapsed > 0 else "< 1m"
        now_str = datetime.now(IST).strftime("%H:%M:%S IST")

        if status == "TARGET_HIT":
            icon = "🏆"
            title = "<b>TARGET HIT — PROFIT BOOKED!</b>"
            pnl_color = f"+₹{pnl:,.2f}"
        elif status == "STOP_HIT":
            icon = "🛑"
            title = "<b>STOP LOSS HIT — POSITION CLOSED</b>"
            pnl_color = f"-₹{abs(pnl):,.2f}"
        elif status == "INVALID_CHASE_PREVENTED":
            icon = "🚫"
            title = "<b>CHASE PREVENTED — RUNAWAY GUARDRAIL</b>"
            pnl_color = "₹0.00 (No Trade Taken)"
        else:
            icon = "🏁"
            title = f"<b>SIGNAL RESOLVED ({status})</b>"
            pnl_color = f"₹{pnl:+,.2f}"

        pts_gained = exit_p - entry

        return (
            f"{icon} {title}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Contract:</b> <code>{symbol}</code>\n"
            f"<b>Strategy:</b> <code>{strategy}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Entry:</b> <code>₹{entry:.2f}</code> ➔ <b>Exit:</b> <code>₹{exit_p:.2f}</code>\n"
            f"<b>Move:</b> <code>{pts_gained:+.2f} pts</code>\n"
            f"💰 <b>Realized PnL:</b> <b>{pnl_color}</b>\n"
            f"⏱️ <b>Duration:</b> <code>{dur_str}</code>\n"
            f"🕒 <i>{now_str}</i>"
        )

    async def notify_new_signal(self, sig: Dict[str, Any]):
        if self.notify_signals:
            msg = self.format_signal_html(sig)
            await self.enqueue_message(msg)

    async def notify_radar_alert(self, alert: Dict[str, Any]):
        if self.notify_radar:
            msg = self.format_radar_html(alert)
            await self.enqueue_message(msg)

    async def notify_signal_resolved(self, sig: Dict[str, Any]):
        if self.notify_resolutions:
            msg = self.format_resolution_html(sig)
            await self.enqueue_message(msg)
