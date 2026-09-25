import asyncio
import json
from typing import AsyncGenerator, Dict, Any, Optional
import redis.asyncio as aioredis
from loguru import logger
import yaml
import os

class RedisBus:
    def __init__(self, host: str = "localhost", port: int = 6379, db: int = 0):
        self.host = host
        self.port = port
        self.db = db
        self.client: Optional[aioredis.Redis] = None
        self.market_channel = "channel:market_ticks"
        self.simulated_channel = "channel:simulated_ticks"
        self.signals_channel = "channel:signals"
        self.telemetry_channel = "channel:telemetry"

    @classmethod
    def from_config(cls, config_path: Optional[str] = None) -> "RedisBus":
        if not config_path:
            config_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "settings.yaml")
        
        host, port, db = "localhost", 6379, 0
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                cfg = yaml.safe_load(f).get("redis", {})
                host = cfg.get("host", host)
                port = cfg.get("port", port)
                db = cfg.get("db", db)
        
        bus = cls(host=host, port=port, db=db)
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                cfg = yaml.safe_load(f).get("redis", {})
                bus.market_channel = cfg.get("market_channel", bus.market_channel)
                bus.simulated_channel = cfg.get("simulated_channel", bus.simulated_channel)
                bus.signals_channel = cfg.get("signals_channel", bus.signals_channel)
                bus.telemetry_channel = cfg.get("telemetry_channel", bus.telemetry_channel)
        return bus

    async def connect(self):
        if not self.client:
            self.client = aioredis.from_url(
                f"redis://{self.host}:{self.port}/{self.db}",
                decode_responses=True,
                encoding="utf-8"
            )
            await self.client.ping()
            logger.info(f"Connected to Redis at {self.host}:{self.port}")

    async def close(self):
        if self.client:
            await self.client.close()
            self.client = None
            logger.info("Redis connection closed.")

    async def publish_tick(self, tick: Dict[str, Any], channel: Optional[str] = None):
        if not self.client:
            await self.connect()
        target_channel = channel or self.market_channel
        payload = json.dumps(tick)
        await self.client.publish(target_channel, payload)

    async def publish_signal(self, signal: Dict[str, Any]):
        if not self.client:
            await self.connect()
        payload = json.dumps(signal)
        await self.client.publish(self.signals_channel, payload)

    async def publish_telemetry(self, telemetry: Dict[str, Any]):
        if not self.client:
            await self.connect()
        payload = json.dumps(telemetry)
        await self.client.publish(self.telemetry_channel, payload)

    async def subscribe(self, *channels: str) -> AsyncGenerator[Dict[str, Any], None]:
        if not self.client:
            await self.connect()
        
        pubsub = self.client.pubsub()
        target_channels = list(channels) if channels else [self.market_channel, self.simulated_channel]
        await pubsub.subscribe(*target_channels)
        logger.info(f"Subscribed to Redis channels: {target_channels}")

        try:
            async for message in pubsub.listen():
                if message and message.get("type") == "message":
                    channel = message.get("channel")
                    try:
                        data = json.loads(message.get("data", "{}"))
                        data["_channel"] = channel
                        yield data
                    except Exception as e:
                        logger.error(f"Error decoding Redis message from {channel}: {e}")
        finally:
            await pubsub.unsubscribe(*target_channels)
            await pubsub.close()
