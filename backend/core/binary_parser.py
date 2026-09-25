import struct
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from loguru import logger

class SmartAPIBinaryParser:
    """
    Parser for AngelOne SmartAPI WebSocket 2.0 binary packet protocol.
    Supports Mode 1 (LTP), Mode 2 (Quote), and Mode 3 (SnapQuote with OI).
    """

    EXCHANGE_MAP = {
        1: "nse_cm",
        2: "nfo_fo",
        3: "bse_cm",
        4: "bfo_fo",
        5: "mcx_fo",
        7: "ncx_fo",
        13: "cde_fo"
    }

    @classmethod
    def parse_packet(cls, packet: bytes) -> Optional[Dict[str, Any]]:
        """
        Parses binary payload according to SmartAPI WebSocket 2.0 spec.
        """
        if not packet or len(packet) < 43:
            return None

        try:
            # Header:
            # subscription_mode: int8 (1 byte)
            # exchange_type: int8 (1 byte)
            # token: 25 bytes char array
            # sequence_number: int64 (8 bytes)
            # exchange_timestamp: int64 (8 bytes)
            mode = packet[0]
            exchange_type_code = packet[1]
            token_raw = packet[2:27]
            token = token_raw.decode("ascii", errors="ignore").strip("\x00").strip()
            
            seq_num, exch_ts = struct.unpack("<qq", packet[27:43])
            exchange = cls.EXCHANGE_MAP.get(exchange_type_code, f"exch_{exchange_type_code}")

            tick = {
                "mode": mode,
                "exchange": exchange,
                "token": token,
                "sequence_number": seq_num,
                "exchange_timestamp": exch_ts,
                "received_at": datetime.now(timezone.utc).isoformat()
            }

            # Mode 1: LTP Mode (43 + 8 = 51 bytes)
            if mode == 1:
                if len(packet) >= 51:
                    ltp_raw = struct.unpack("<q", packet[43:51])[0]
                    tick["ltp"] = ltp_raw / 100.0
                return tick

            # Mode 2: Quote Mode (43 + 72 = 115 bytes)
            if mode in (2, 3) and len(packet) >= 115:
                (
                    ltp_raw,
                    last_qty,
                    avg_price_raw,
                    vol_today,
                    total_buy_qty,
                    total_sell_qty,
                    open_raw,
                    high_raw,
                    low_raw,
                    close_raw
                ) = struct.unpack("<qqqqqqqqqq", packet[43:123])

                tick.update({
                    "ltp": ltp_raw / 100.0,
                    "last_traded_qty": last_qty,
                    "avg_traded_price": avg_price_raw / 100.0,
                    "volume": vol_today,
                    "total_buy_qty": total_buy_qty,
                    "total_sell_qty": total_sell_qty,
                    "open": open_raw / 100.0,
                    "high": high_raw / 100.0,
                    "low": low_raw / 100.0,
                    "close": close_raw / 100.0,
                })

            # Mode 3: SnapQuote Mode (contains Open Interest)
            # Mode 3 extends Mode 2 with last_traded_ts (8), open_interest (8), etc.
            if mode == 3 and len(packet) >= 147:
                last_trade_ts, oi, oi_high, oi_low = struct.unpack("<qqqq", packet[123:155])
                tick.update({
                    "last_traded_timestamp": last_trade_ts,
                    "open_interest": oi,
                    "oi_day_high": oi_high,
                    "oi_day_low": oi_low
                })

            return tick

        except Exception as e:
            logger.debug(f"Failed to parse binary packet (len={len(packet)}): {e}")
            return None
