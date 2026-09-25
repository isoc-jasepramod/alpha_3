import os
import json
import httpx
from datetime import datetime, timezone, date
from typing import Dict, Any, List, Optional, Tuple
from loguru import logger
import yaml

class InstrumentManager:
    """
    Manages AngelOne Instrument Master scrip list.
    Dynamically maps ATM, ITM, and OTM strikes for NIFTY and SENSEX,
    resolves active weekly expiries, and tracks dynamic strike migration.
    """

    def __init__(self, cache_dir: str = "data"):
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cache_file = os.path.join(self.cache_dir, "instrument_master.json")
        self.instruments: List[Dict[str, Any]] = []
        self.contracts_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
        self.current_atm: Dict[str, float] = {}

        # Default rules
        self.rules = {
            "NIFTY": {
                "strike_interval": 50,
                "spot_token": "99926000",
                "spot_exchange": "nse_cm",
                "opt_exchange": "nfo_fo",
                "default_lot_size": 65
            },
            "SENSEX": {
                "strike_interval": 100,
                "spot_token": "99919000",
                "spot_exchange": "bse_cm",
                "opt_exchange": "bfo_fo",
                "default_lot_size": 20
            }
        }
        self._load_rules_config()

    def _load_rules_config(self):
        config_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "market_rules.yaml")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    cfg = yaml.safe_load(f)
                    inst_cfg = cfg.get("instruments", {})
                    for sym, data in inst_cfg.items():
                        if sym in self.rules:
                            self.rules[sym].update(data)
            except Exception as e:
                logger.warning(f"Could not load market_rules.yaml: {e}")

    async def sync_master(self, url: Optional[str] = None, force_download: bool = False) -> bool:
        """
        Downloads AngelOne master JSON if forced or if cache is older than today.
        """
        master_url = url or "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        
        # Check if local cache exists and is fresh
        if not force_download and os.path.exists(self.cache_file):
            try:
                mod_time = datetime.fromtimestamp(os.path.getmtime(self.cache_file), tz=timezone.utc).date()
                if mod_time == datetime.now(timezone.utc).date():
                    logger.info("Using cached Instrument Master JSON from today.")
                    return self._load_from_cache()
            except Exception as e:
                logger.warning(f"Error checking cache timestamp: {e}")

        logger.info(f"Downloading Instrument Master JSON from {master_url}...")
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(master_url)
                if resp.status_code == 200:
                    with open(self.cache_file, "wb") as f:
                        f.write(resp.content)
                    logger.info(f"Downloaded and cached Instrument Master ({len(resp.content) / 1024 / 1024:.2f} MB)")
                    return self._load_from_cache()
                else:
                    logger.error(f"HTTP error downloading master: {resp.status_code}")
        except Exception as e:
            logger.warning(f"Failed to download Instrument Master online: {e}. Falling back to existing cache or mock.")

        return self._load_from_cache()

    def _load_from_cache(self) -> bool:
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.instruments = data
                    self._index_instruments()
                    logger.info(f"Indexed {len(self.instruments)} instruments.")
                    return True
            except Exception as e:
                logger.error(f"Failed to load cache: {e}")

        # Fallback to local mock table for simulation if online file cannot be loaded
        self._generate_fallback_instruments()
        return True

    def _generate_fallback_instruments(self):
        """Generates realistic synthetic options chain for NIFTY and SENSEX when offline"""
        logger.info("Generating synthetic option chain for offline/simulation mode...")
        mock_instruments = []
        today = date.today()
        # NIFTY spot token
        mock_instruments.append({
            "token": "99926000",
            "symbol": "Nifty 50",
            "name": "NIFTY",
            "expiry": "",
            "strike": "-1.000000",
            "lotsize": "1",
            "instrumenttype": "AMXIDX",
            "exch_seg": "nse_cm"
        })
        # SENSEX spot token
        mock_instruments.append({
            "token": "99919000",
            "symbol": "SENSEX",
            "name": "SENSEX",
            "expiry": "",
            "strike": "-1.000000",
            "lotsize": "1",
            "instrumenttype": "AMXIDX",
            "exch_seg": "bse_cm"
        })

        # Generate NIFTY strikes around 25000 (24000 to 26000)
        token_id = 100000
        for strike in range(24000, 26050, 50):
            for opt_type in ["CE", "PE"]:
                token_id += 1
                mock_instruments.append({
                    "token": str(token_id),
                    "symbol": f"NIFTY{today.strftime('%d%b%y').upper()}{strike}{opt_type}",
                    "name": "NIFTY",
                    "expiry": today.strftime("%d%b%Y").upper(),
                    "strike": f"{float(strike * 100):.6f}",
                    "lotsize": "65",
                    "instrumenttype": "OPTIDX",
                    "exch_seg": "nfo_fo"
                })

        # Generate SENSEX strikes around 82000 (80000 to 84000)
        for strike in range(80000, 84100, 100):
            for opt_type in ["CE", "PE"]:
                token_id += 1
                mock_instruments.append({
                    "token": str(token_id),
                    "symbol": f"SENSEX{today.strftime('%d%b%y').upper()}{strike}{opt_type}",
                    "name": "SENSEX",
                    "expiry": today.strftime("%d%b%Y").upper(),
                    "strike": f"{float(strike * 100):.6f}",
                    "lotsize": "20",
                    "instrumenttype": "OPTIDX",
                    "exch_seg": "bfo_fo"
                })

        self.instruments = mock_instruments
        self._index_instruments()

    def _index_instruments(self):
        self.contracts_by_symbol = {"NIFTY": [], "SENSEX": []}
        for item in self.instruments:
            name = item.get("name", "")
            if name in self.contracts_by_symbol and item.get("instrumenttype") == "OPTIDX":
                self.contracts_by_symbol[name].append(item)

    def get_nearest_expiry(self, symbol: str) -> Optional[str]:
        contracts = self.contracts_by_symbol.get(symbol, [])
        if not contracts:
            return None
        
        # Unique expiries
        expiries = set()
        for c in contracts:
            exp = c.get("expiry", "").strip()
            if exp:
                expiries.add(exp)
        
        if not expiries:
            return None

        # Parse and find nearest active expiry
        parsed = []
        today = date.today()
        for exp_str in expiries:
            for fmt in ("%d%b%Y", "%d-%b-%Y", "%d%B%Y"):
                try:
                    dt = datetime.strptime(exp_str, fmt).date()
                    if dt >= today:
                        parsed.append((dt, exp_str))
                    break
                except ValueError:
                    continue

        if parsed:
            parsed.sort(key=lambda x: x[0])
            return parsed[0][1]

        # If all expired, return first available
        return list(expiries)[0]

    def calculate_atm_strike(self, symbol: str, spot_price: float) -> float:
        interval = self.rules.get(symbol, {}).get("strike_interval", 50)
        return round(spot_price / interval) * interval

    def get_atm_and_wings(
        self,
        symbol: str,
        spot_price: float,
        strikes_range: List[int] = [-2, -1, 0, 1, 2]
    ) -> Dict[str, Any]:
        """
        Returns ATM and wings mapping for CE and PE contracts:
        ATM-2, ATM-1, ATM, ATM+1, ATM+2
        """
        atm_strike = self.calculate_atm_strike(symbol, spot_price)
        self.current_atm[symbol] = atm_strike
        interval = self.rules.get(symbol, {}).get("strike_interval", 50)
        nearest_expiry = self.get_nearest_expiry(symbol)

        contracts = self.contracts_by_symbol.get(symbol, [])
        filtered_contracts = [
            c for c in contracts if c.get("expiry", "").strip() == nearest_expiry
        ]

        # Map by (strike, option_type)
        lookup = {}
        for c in filtered_contracts:
            try:
                # Raw strike in Angel master is strike * 100
                raw_strike = float(c.get("strike", 0.0))
                strike_val = raw_strike / 100.0 if raw_strike > 100000 else raw_strike
                sym_code = c.get("symbol", "")
                opt_type = "CE" if sym_code.endswith("CE") else "PE" if sym_code.endswith("PE") else None
                if opt_type:
                    lookup[(strike_val, opt_type)] = c
            except Exception:
                continue

        result = {
            "symbol": symbol,
            "spot_price": spot_price,
            "atm_strike": atm_strike,
            "expiry": nearest_expiry,
            "tokens": {},      # token -> metadata
            "by_strike": {}    # strike -> {CE: meta, PE: meta}
        }

        for offset in strikes_range:
            target_strike = atm_strike + (offset * interval)
            result["by_strike"][target_strike] = {}
            for opt_type in ["CE", "PE"]:
                c = lookup.get((target_strike, opt_type))
                if c:
                    lot_size = int(c.get("lotsize", self.rules.get(symbol, {}).get("default_lot_size", 50)))
                    meta = {
                        "token": c.get("token"),
                        "symbol": c.get("symbol"),
                        "name": symbol,
                        "strike": target_strike,
                        "option_type": opt_type,
                        "expiry": nearest_expiry,
                        "lot_size": lot_size,
                        "exchange": c.get("exch_seg", self.rules.get(symbol, {}).get("opt_exchange", "nfo_fo")),
                        "offset": offset
                    }
                    result["by_strike"][target_strike][opt_type] = meta
                    result["tokens"][meta["token"]] = meta

        return result

    def detect_atm_migration(self, symbol: str, spot_price: float) -> Optional[float]:
        """
        Checks if spot price moved enough to shift ATM to a new strike.
        """
        curr_atm = self.current_atm.get(symbol)
        if curr_atm is None:
            new_atm = self.calculate_atm_strike(symbol, spot_price)
            self.current_atm[symbol] = new_atm
            return new_atm

        interval = self.rules.get(symbol, {}).get("strike_interval", 50)
        new_atm = round(spot_price / interval) * interval
        if new_atm != curr_atm:
            logger.info(f"ATM migration detected for {symbol}: {curr_atm} -> {new_atm} (Spot: {spot_price})")
            self.current_atm[symbol] = new_atm
            return new_atm
        return None
