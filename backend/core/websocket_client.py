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
