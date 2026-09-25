import os
import pyotp
from dotenv import load_dotenv
from typing import Dict, Any, Optional
from loguru import logger
from SmartApi import SmartConnect

# Load .env
dotenv_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
load_dotenv(dotenv_path)

class AngelOneAuth:
    """
    Authenticates with AngelOne SmartAPI using official SmartConnect SDK and TOTP.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        client_code: Optional[str] = None,
        pin: Optional[str] = None,
        totp_secret: Optional[str] = None
    ):
        self.api_key = (api_key or os.getenv("ANGEL_API_KEY", "")).strip().strip('"').strip("'")
        self.client_code = (client_code or os.getenv("ANGEL_CLIENT_CODE", "")).strip().strip('"').strip("'")
        self.pin = (pin or os.getenv("ANGEL_PIN", "")).strip().strip('"').strip("'")
        self.totp_secret = (totp_secret or os.getenv("ANGEL_TOTP_TOKEN", "")).strip().strip('"').strip("'")

        self.smart_connect: Optional[SmartConnect] = None
        self.jwt_token: Optional[str] = None
        self.feed_token: Optional[str] = None
        self.user_name: Optional[str] = None

    def generate_totp(self) -> str:
        if not self.totp_secret:
            raise ValueError("TOTP secret is empty.")
        totp = pyotp.TOTP(self.totp_secret)
        return totp.now()

    def login_sync(self) -> Dict[str, Any]:
        """
        Authenticates with AngelOne and retrieves active tokens.
        """
        if not self.api_key or not self.client_code or not self.pin or not self.totp_secret:
            logger.warning("AngelOne credentials incomplete in .env. Running in standalone/lab mode.")
            return {"status": False, "message": "Incomplete credentials"}

        try:
            totp_code = self.generate_totp()
            logger.info(f"Authenticating AngelOne SmartAPI for {self.client_code}...")
            self.smart_connect = SmartConnect(api_key=self.api_key)
            session = self.smart_connect.generateSession(self.client_code, self.pin, totp_code)

            if session.get("status"):
                data = session.get("data", {})
                self.jwt_token = data.get("jwtToken")
                self.feed_token = data.get("feedToken")
                self.user_name = data.get("name", self.client_code)
                logger.success(f"Connected to AngelOne SmartAPI for '{self.user_name}'! (FeedToken generated)")
                return {
                    "status": True,
                    "jwt_token": self.jwt_token,
                    "feed_token": self.feed_token,
                    "user_name": self.user_name
                }
            else:
                msg = session.get("message", "Authentication error")
                logger.error(f"AngelOne login rejected: {msg}")
                return {"status": False, "message": msg}

        except Exception as e:
            logger.error(f"Exception during AngelOne login: {e}")
            return {"status": False, "message": str(e)}

    async def login(self) -> Dict[str, Any]:
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.login_sync)
