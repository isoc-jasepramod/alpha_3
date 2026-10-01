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
                # NOTE: total_buy_qty and total_sell_qty are float64 (doubles) in the
                # AngelOne SmartAPI binary feed — every OTHER field here is int64. Reading
                # them as int64 produced garbage (~4.7e18), which silently broke the
                # FlowEngine order-flow logic (queue_ratio pinned to ~1.0 all day).
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
                ) = struct.unpack("<qqqqddqqqq", packet[43:123])

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

            # Mode 3: SnapQuote Mode (contains Open Interest + best-5 market depth)
            if mode == 3 and len(packet) >= 147:
                last_trade_ts, oi, oi_high, oi_low = struct.unpack("<qqqq", packet[123:155])
                tick.update({
                    "last_traded_timestamp": last_trade_ts,
                    "open_interest": oi,
                    "oi_day_high": oi_high,
                    "oi_day_low": oi_low
                })

            # Best-5 market depth (bid/ask). The 379-byte SnapQuote packet carries a 200-byte
            # "best twenty" (best-five each side) block at offset 155 (right after the OI block):
            # 10 entries x 20 bytes. First 5 entries = BUY (bid) side, next 5 = SELL (ask) side.
            # Each 20-byte entry = int16 buy/sell flag, int64 quantity, int64 price(*100),
            # int16 number-of-orders, + 2 pad bytes to align to 20.
            #
            # ⚠️ VERIFY-BEFORE-TRUST: these byte offsets follow the documented spec but have NOT
            # yet been confirmed against a real live Mode-3 packet on this account. Confirm with
            # cls.verify_depth_offsets(raw_packet) on live data (sanity: bid1 <= ltp <= ask1,
            # quantities > 0) before relying on depth for trade classification. This is exactly
            # how the total_buy/sell_qty int64-vs-double bug slipped through before.
            if mode == 3 and len(packet) >= 355:
                depth = cls._parse_best_five(packet)
                if depth:
                    tick["depth"] = depth
                    # Convenience top-of-book fields for the FlowEngine trade classifier.
                    if depth["bids"]:
                        tick["best_bid"] = depth["bids"][0]["price"]
                        tick["best_bid_qty"] = depth["bids"][0]["qty"]
                    if depth["asks"]:
                        tick["best_ask"] = depth["asks"][0]["price"]
                        tick["best_ask_qty"] = depth["asks"][0]["qty"]

            return tick

        except Exception as e:
            logger.debug(f"Failed to parse binary packet (len={len(packet)}): {e}")
            return None

    # 20-byte best-five entry layout: int16 flag, int64 qty, int64 price(*100), int16 numOrders.
    # Offset 147 CONFIRMED against a live option packet (decode_depth: clean bid/ask ladder
    # 70.90..70.70 / 71.10..71.30 with realistic qtys & order counts). First 5 = bid, next 5 = ask.
    _DEPTH_OFFSET = 147
    _DEPTH_ENTRY_SIZE = 20
    _DEPTH_LEVELS = 5

    @classmethod
    def _parse_best_five(cls, packet: bytes) -> Optional[Dict[str, Any]]:
        """
        Decodes the 200-byte best-five depth block (10 entries: 5 bid + 5 ask).
        Returns {"bids": [{price, qty, orders}...], "asks": [...]} with prices in rupees.
        """
        try:
            base = cls._DEPTH_OFFSET
            bids, asks = [], []
            for i in range(cls._DEPTH_LEVELS * 2):
                off = base + i * cls._DEPTH_ENTRY_SIZE
                if off + cls._DEPTH_ENTRY_SIZE > len(packet):
                    break
                flag = struct.unpack("<h", packet[off:off + 2])[0]
                qty = struct.unpack("<q", packet[off + 2:off + 10])[0]
                price_raw = struct.unpack("<q", packet[off + 10:off + 18])[0]
                orders = struct.unpack("<h", packet[off + 18:off + 20])[0]
                entry = {"price": price_raw / 100.0, "qty": qty, "orders": orders}
                # flag == 1 => buy (bid) side per spec; first 5 are buy, next 5 sell.
                if i < cls._DEPTH_LEVELS:
                    bids.append(entry)
                else:
                    asks.append(entry)
            if not bids and not asks:
                return None
            return {"bids": bids, "asks": asks}
        except Exception:
            return None

    @classmethod
    def verify_depth_offsets(cls, packet: bytes) -> Dict[str, Any]:
        """
        Live-verification helper. Parse a REAL Mode-3 packet and return a sanity report so we
        can confirm the depth byte offsets before trusting them:
          - bid1 <= ltp <= ask1 (top-of-book brackets the last price)
          - positive quantities, monotonic bid (desc) / ask (asc) prices
        Call this on a captured live packet; do NOT trust depth until this passes.
        """
        t = cls.parse_packet(packet)
        if not t or "depth" not in t:
            return {"ok": False, "reason": "no depth decoded", "tick": t}
        d = t["depth"]
        ltp = t.get("ltp", 0.0)
        bid1 = d["bids"][0]["price"] if d["bids"] else None
        ask1 = d["asks"][0]["price"] if d["asks"] else None
        # bid1 <= ask1 (valid book) is the hard invariant. ltp sitting exactly between the
        # touch is NOT required: ltp is the last TRADE price and the quote can move past it
        # between trades, so ltp may be at/just outside bid1/ask1 legitimately.
        checks = {
            "book_ordered": (bid1 is not None and ask1 is not None and bid1 <= ask1),
            "ltp_near_book": (bid1 is not None and ask1 is not None and (bid1 * 0.98) <= ltp <= (ask1 * 1.02)),
            "bids_positive_qty": all(b["qty"] >= 0 for b in d["bids"]),
            "asks_positive_qty": all(a["qty"] >= 0 for a in d["asks"]),
            "bids_desc": all(d["bids"][i]["price"] >= d["bids"][i + 1]["price"] for i in range(len(d["bids"]) - 1)) if len(d["bids"]) > 1 else True,
            "asks_asc": all(d["asks"][i]["price"] <= d["asks"][i + 1]["price"] for i in range(len(d["asks"]) - 1)) if len(d["asks"]) > 1 else True,
        }
        return {"ok": all(checks.values()), "checks": checks, "ltp": ltp, "bid1": bid1, "ask1": ask1, "depth": d}
