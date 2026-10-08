"""
PROJECT ALPHA 3.0 — MARKET BREADTH ENGINE
=========================================
Fetches index-constituent heavyweights via AngelOne SmartAPI getMarketData("FULL", ...) in a
single batch call and computes participation metrics for the Regime Filter v2:
  - breadth %      : fraction of the basket advancing
  - weighted pressure %: index-weight-weighted sum of per-stock % change
  - spoof flag     : one heavyweight skewing the index while breadth is weak

Data is LIVE-ONLY (polled REST). There is no recorded constituent history, so the breadth /
participation dimension can only be validated going forward, not backtested on the data lake.

Weights are a configurable snapshot of index composition and drift over time (rebalances /
price moves). Breadth (advance/decline count) is robust to stale weights; weighted_pressure is
more sensitive — treat weights as "refresh periodically", not gospel.
"""
from dataclasses import dataclass, asdict
from typing import Dict, List, Any, Optional
from loguru import logger


# Top heavyweights per index. token = exchange symboltoken used by getMarketData.
# NIFTY/BANKNIFTY constituents live on NSE; SENSEX uses BSE scrip codes (with NSE fallback token).
NIFTY_CONSTITUENTS: List[Dict[str, Any]] = [
    {"symbol": "HDFCBANK",   "token": "1333",  "weight": 0.135},
    {"symbol": "RELIANCE",   "token": "2885",  "weight": 0.102},
    {"symbol": "ICICIBANK",  "token": "4963",  "weight": 0.088},
    {"symbol": "INFY",       "token": "1594",  "weight": 0.059},
    {"symbol": "TCS",        "token": "11536", "weight": 0.048},
    {"symbol": "ITC",        "token": "1660",  "weight": 0.041},
    {"symbol": "LT",         "token": "11483", "weight": 0.039},
    {"symbol": "BHARTIARTL", "token": "10604", "weight": 0.036},
    {"symbol": "AXISBANK",   "token": "5900",  "weight": 0.034},
    {"symbol": "KOTAKBANK",  "token": "1922",  "weight": 0.029},
]

# SENSEX: BSE scrip codes as the primary token; nse_token kept as a liquidity fallback.
SENSEX_CONSTITUENTS: List[Dict[str, Any]] = [
    {"symbol": "HDFCBANK",   "token": "500180", "nse_token": "1333",  "weight": 0.152},
    {"symbol": "RELIANCE",   "token": "500325", "nse_token": "2885",  "weight": 0.118},
    {"symbol": "ICICIBANK",  "token": "532174", "nse_token": "4963",  "weight": 0.092},
    {"symbol": "INFY",       "token": "500209", "nse_token": "1594",  "weight": 0.068},
    {"symbol": "TCS",        "token": "532540", "nse_token": "11536", "weight": 0.055},
    {"symbol": "ITC",        "token": "500875", "nse_token": "1660",  "weight": 0.047},
    {"symbol": "LT",         "token": "500510", "nse_token": "11483", "weight": 0.044},
    {"symbol": "BHARTIARTL", "token": "532454", "nse_token": "10604", "weight": 0.042},
    {"symbol": "AXISBANK",   "token": "532215", "nse_token": "5900",  "weight": 0.038},
    {"symbol": "SBIN",       "token": "500112", "nse_token": "3045",  "weight": 0.035},
]

CONSTITUENTS = {"NIFTY": NIFTY_CONSTITUENTS, "SENSEX": SENSEX_CONSTITUENTS}
EXCHANGE_FOR = {"NIFTY": "NSE", "SENSEX": "BSE"}

ADV_DECL_THRESHOLD = 0.0002  # +/-0.02% dead-band for advance/decline classification


@dataclass
class BreadthResult:
    breadth_pct: float            # 0.0..1.0 fraction advancing
    advances: int
    declines: int
    neutrals: int
    total_stocks: int
    weighted_pressure_pct: float  # weight-weighted sum of % change, in %
    is_bullish: bool              # breadth >= 0.65
    is_bearish: bool              # breadth <= 0.35
    spoof_detected: bool          # single stock skewing index while breadth weak
    valid: bool                   # True if we had enough constituent quotes to trust it

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _neutral_result(valid: bool = False) -> BreadthResult:
    return BreadthResult(0.5, 0, 0, 0, 0, 0.0, False, False, False, valid)


class MarketBreadthEngine:
    """Pure computation: turn a getMarketData('FULL', ...) 'fetched' list into breadth metrics."""

    @staticmethod
    def compute(fetched_items: List[dict], constituents: List[dict]) -> BreadthResult:
        quotes = {str(it.get("symbolToken")): it for it in (fetched_items or [])}
        advances = declines = neutrals = valid_count = 0
        weighted_sum = 0.0
        total_weight = 0.0
        pressures = []  # (symbol, pct_change, weight)

        for c in constituents:
            q = quotes.get(str(c["token"]))
            if not q:
                continue
            try:
                ltp = float(q.get("ltp", 0.0))
                close = float(q.get("close", 0.0))
            except (TypeError, ValueError):
                continue
            if ltp <= 0 or close <= 0:
                continue
            valid_count += 1
            w = float(c["weight"])
            total_weight += w
            pct = (ltp - close) / close
            weighted_sum += w * pct
            pressures.append((c["symbol"], pct, w))
            if pct > ADV_DECL_THRESHOLD:
                advances += 1
            elif pct < -ADV_DECL_THRESHOLD:
                declines += 1
            else:
                neutrals += 1

        # Require a quorum (>= half the basket) before trusting breadth.
        if valid_count == 0 or valid_count < max(3, len(constituents) // 2):
            return _neutral_result(valid=False)

        breadth_pct = advances / valid_count
        weighted_pressure = (weighted_sum / total_weight) * 100.0 if total_weight > 0 else 0.0

        spoof = False
        if pressures and abs(weighted_sum) > 0.0005:
            top = max(pressures, key=lambda s: abs(s[1] * s[2]))
            top_impact = top[1] * top[2]
            if weighted_sum != 0 and (top_impact / weighted_sum) > 0.65 and breadth_pct < 0.50:
                spoof = True

        return BreadthResult(
            breadth_pct=round(breadth_pct, 3),
            advances=advances, declines=declines, neutrals=neutrals,
            total_stocks=valid_count,
            weighted_pressure_pct=round(weighted_pressure, 3),
            is_bullish=(breadth_pct >= 0.65),
            is_bearish=(breadth_pct <= 0.35),
            spoof_detected=spoof,
            valid=True,
        )


class BreadthPoller:
    """
    Polls getMarketData('FULL', ...) for NIFTY + SENSEX heavyweights on a cadence and caches the
    latest BreadthResult per instrument. One batch call per exchange (no per-stock loop) to avoid
    SmartAPI rate limits. Degrades gracefully (keeps last good result, marks stale) on error.
    """

    def __init__(self, auth, poll_interval_sec: int = 45):
        self.auth = auth
        self.poll_interval = poll_interval_sec
        self.latest: Dict[str, BreadthResult] = {
            "NIFTY": _neutral_result(False),
            "SENSEX": _neutral_result(False),
        }
        self.last_poll_ts: Dict[str, float] = {"NIFTY": 0.0, "SENSEX": 0.0}
        self._running = False

    def _fetch_one(self, inst: str) -> Optional[BreadthResult]:
        sc = getattr(self.auth, "smart_connect", None)
        if sc is None:
            return None
        cons = CONSTITUENTS[inst]
        exch = EXCHANGE_FOR[inst]
        tokens = [c["token"] for c in cons]
        try:
            resp = sc.getMarketData("FULL", {exch: tokens})
            fetched = (resp or {}).get("data", {}).get("fetched", []) if isinstance(resp, dict) else []
            if not fetched:
                return None
            return MarketBreadthEngine.compute(fetched, cons)
        except Exception as e:
            logger.warning(f"[BREADTH] {inst} getMarketData failed: {e}")
            return None

    async def poll_once(self):
        import asyncio
        from datetime import datetime, timezone
        loop = asyncio.get_running_loop()
        for inst in ("NIFTY", "SENSEX"):
            res = await loop.run_in_executor(None, self._fetch_one, inst)
            if res and res.valid:
                self.latest[inst] = res
                self.last_poll_ts[inst] = datetime.now(timezone.utc).timestamp()
                logger.debug(
                    f"[BREADTH] {inst}: {res.breadth_pct*100:.0f}% adv ({res.advances}/{res.total_stocks}), "
                    f"wpress {res.weighted_pressure_pct:+.2f}%, spoof={res.spoof_detected}"
                )

    async def start(self):
        import asyncio
        self._running = True
        logger.info(f"[BREADTH] Poller started (interval {self.poll_interval}s).")
        while self._running:
            try:
                await self.poll_once()
            except Exception as e:
                logger.warning(f"[BREADTH] poll loop error: {e}")
            await asyncio.sleep(self.poll_interval)

    def stop(self):
        self._running = False

    def get(self, inst: str) -> BreadthResult:
        return self.latest.get(inst, _neutral_result(False))
