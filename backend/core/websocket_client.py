import asyncio
import json
import websockets
import random
from typing import Dict, Any, List, Optional
from loguru import logger
from backend.core.binary_parser import SmartAPIBinaryParser
from backend.core.redis_bus import RedisBus

class SmartAPIWebSocketClient:
    """
    Connects to AngelOne SmartAPI WebSocket 2.0 (Binary stream)
    and broadcasts parsed ticks to Redis channel:market_ticks.
    Includes auto-reconnect and subscription management.
    """

    WS_URL = "wss://smartapisocket.angelone.in/smart-stream"

    def __init__(
        self,
        redis_bus: RedisBus,
        auth_token: str = "",
        api_key: str = "",
        client_code: str = "",
        feed_token: str = ""
    ):
        self.redis_bus = redis_bus
        self.auth_token = auth_token
        self.api_key = api_key
        self.client_code = client_code
        self.feed_token = feed_token
        self.running = False
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.subscribed_tokens: Dict[str, List[str]] = {} # exchange -> [tokens]
        self._task: Optional[asyncio.Task] = None
        # One-shot depth-offset verification: on the first real Mode-3 SnapQuote packet with
        # depth, run verify_depth_offsets() on the raw bytes, write a report, then disable.
        # Confirms the FlowEngine v2 best-5 byte offsets on LIVE data (no second WS session).
        self._depth_verify_done = False

    def _verify_depth_once(self, raw_packet: bytes):
        """
        One-shot: run verify_depth_offsets() on the first live Mode-3 depth packet, log the
        result, and write it to logs/<date>/depth_verify.json. Confirms the FlowEngine v2
        best-5 byte offsets decode correctly on REAL data. Self-disables after one run.
        """
        self._depth_verify_done = True  # set first so a parse error can't retrigger a loop
        try:
            import os, json
            from datetime import datetime, timezone
            rep = SmartAPIBinaryParser.verify_depth_offsets(raw_packet)
            status = "PASS" if rep.get("ok") else "FAIL"
            logger.warning(
                f"🔎 [DEPTH VERIFY] {status} offsets check on live packet "
                f"(ltp={rep.get('ltp')} bid1={rep.get('bid1')} ask1={rep.get('ask1')}) checks={rep.get('checks')}"
            )
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            out_dir = os.path.join("logs", today)
            os.makedirs(out_dir, exist_ok=True)
            rep_out = dict(rep)
            rep_out["packet_len"] = len(raw_packet)
            rep_out["written_at"] = datetime.now(timezone.utc).isoformat()
            rep_out["raw_hex"] = raw_packet.hex()
            with open(os.path.join(out_dir, "depth_verify.json"), "w") as f:
                json.dump(rep_out, f, indent=2, default=str)
            logger.warning(f"🔎 [DEPTH VERIFY] report written to {os.path.join(out_dir, 'depth_verify.json')}")
        except Exception as e:
            logger.warning(f"🔎 [DEPTH VERIFY] failed: {e}")

    def set_tokens(self, subscription_dict: Dict[str, List[str]]):
        """
        subscription_dict format: {"nse_cm": ["99926000"], "nfo_fo": ["100001", "100002"]}
        """
        self.subscribed_tokens = subscription_dict

    async def update_subscriptions(self, subscription_dict: Dict[str, List[str]]):
        """
        Dynamically updates subscribed tokens and sends subscription message over active WebSocket.
        """
        self.subscribed_tokens = subscription_dict
        if self.ws:
            try:
                await self._send_subscriptions(self.ws)
                logger.info("Dynamically updated AngelOne WebSocket subscriptions for migrated strikes.")
            except Exception as e:
                logger.error(f"Failed to send dynamic WebSocket subscriptions: {e}")

    async def start(self):
        self.running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("SmartAPI WebSocket client task started.")

    async def stop(self):
        self.running = False
        if self.ws:
            await self.ws.close()
        if self._task:
            self._task.cancel()
        logger.info("SmartAPI WebSocket client stopped.")

    async def _heartbeat_loop(self, ws):
        """Sends application-level ping every 25 seconds to keep connection alive without library ping_timeout aborts."""
        try:
            while self.running and not ws.closed:
                await asyncio.sleep(25)
                if not ws.closed:
                    await ws.send("ping")
                    logger.debug("SmartAPI WS sent app-level ping heartbeat.")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"Heartbeat loop stopped: {e}")

    async def _run_loop(self):
        attempt = 0
        while self.running:
            # Check if live credentials are provided
            if not self.auth_token and not self.feed_token:
                logger.info("No AngelOne live credentials detected. WebSocket client idle. (Use Replay Engine for testing).")
                # Wait until stopped or awakened
                while self.running and not self.auth_token:
                    await asyncio.sleep(5)
                continue

            heartbeat_task = None
            try:
                headers = {
                    "Authorization": f"Bearer {self.auth_token}",
                    "x-api-key": self.api_key,
                    "x-client-code": self.client_code,
                    "x-feed-token": self.feed_token
                }
                logger.info(f"Connecting to AngelOne SmartAPI WebSocket at {self.WS_URL} (Attempt {attempt + 1})...")
                # Disable library-level protocol ping/pong timeouts (ping_interval=None, ping_timeout=None)
                # to prevent disconnects every 50 seconds when Angel's gateway does not return opcode 0xA pongs
                async with websockets.connect(
                    self.WS_URL,
                    additional_headers=headers,
                    ping_interval=None,
                    ping_timeout=None,
                    close_timeout=10
                ) as ws:
                    self.ws = ws
                    logger.info("Connected to AngelOne SmartAPI WebSocket 2.0.")
                    attempt = 0  # Reset backoff on successful connect
                    await self._send_subscriptions(ws)

                    # Start application-level heartbeat
                    heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))

                    async for message in ws:
                        if not self.running:
                            break
                        if isinstance(message, bytes):
                            # One-shot live depth-offset verification (self-disabling).
                            # Only on OPTION packets (exchange 2=NFO/4=BFO); index has no book.
                            if (not self._depth_verify_done and len(message) >= 355
                                    and message[0] == 3 and message[1] in (2, 4)):
                                self._verify_depth_once(message)
                            tick = SmartAPIBinaryParser.parse_packet(message)
                            if tick:
                                await self.redis_bus.publish_tick(tick)
                        elif isinstance(message, str):
                            # Handle text/pong or status messages
                            if message.lower() in ("pong", "heartbeat"):
                                logger.debug(f"SmartAPI WS received: {message}")
                            else:
                                logger.debug(f"SmartAPI WS text: {message}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                attempt += 1
                delay = min(20.0, (1.5 ** min(attempt, 6)) + random.uniform(0.5, 1.5))
                logger.warning(f"WebSocket connection error: {e}. Reconnecting in {delay:.1f}s (attempt {attempt})...")
                await asyncio.sleep(delay)
            finally:
                if heartbeat_task and not heartbeat_task.done():
                    heartbeat_task.cancel()

    async def _send_subscriptions(self, ws):
        """
        Sends subscription requests for Mode 3 (SnapQuote)
        """
        exchange_code_map = {
            "nse_cm": 1,
            "nfo_fo": 2,
            "bse_cm": 3,
            "bfo_fo": 4
        }
        for exch, tokens in self.subscribed_tokens.items():
            if not tokens:
                continue
            exch_code = exchange_code_map.get(exch, 1)
            sub_msg = {
                "correlationID": f"sub_{exch}",
                "action": 1, # Subscribe
                "params": {
                    "mode": 3, # SnapQuote mode (includes OI and Depth)
                    "tokenList": [
                        {"exchangeType": exch_code, "tokens": tokens}
                    ]
                }
            }
            await ws.send(json.dumps(sub_msg))
            logger.info(f"Subscribed to {len(tokens)} tokens on exchange {exch}.")
