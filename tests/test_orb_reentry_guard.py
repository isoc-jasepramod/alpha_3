import pytest
from datetime import datetime, timezone, timedelta
from backend.strategies.orb_breakout import VolumeBackedORB

IST = timezone(timedelta(hours=5, minutes=30))


class FakeRegime:
    """Minimal stand-in for RegimeFilter with a settable regime."""
    def __init__(self, regime="NEUTRAL", score=55.0):
        self._regime = regime
        self._score = score

    def set(self, regime, score=70.0):
        self._regime = regime
        self._score = score

    def get_regime(self, inst):
        return {"regime": self._regime, "score": self._score, "direction": "NEUTRAL"}


def _opt_meta():
    return {"is_spot": False, "name": "NIFTY", "option_type": "PE",
            "strike": 22700.0, "lot_size": 65, "offset": 0, "symbol": "NIFTY22700PE"}


def _prime_pending_pe(strat, inst="NIFTY"):
    """Directly set a pending PE breakout so we can test the firing guards."""
    strat.orb_ranges[inst] = {
        "high": 22800.0, "low": 22705.0, "finalized": True,
        "pending_signal": {
            "direction": "PE", "close": 22690.0, "open": 22710.0,
            "h_orb": 22800.0, "l_orb": 22705.0, "orb_range": 95.0,
            "adaptive_sl": 22737.5, "delta": 6.8,
            "volume_contact": {"boost_score": 0, "is_contact": False},
            "volume_dryup": {"boost_score": 0, "is_ignition": False},
            "time": 0.0,
        },
    }


@pytest.mark.asyncio
async def test_orb_reentry_cap_blocks_repeat_fires():
    """
    Regression for the 29-Sep incident: ORB fired the SAME PE breakout 20 times.
    With max_triggers_per_dir=2, only 2 should fire; the rest are suppressed.
    """
    from collections import deque
    strat = VolumeBackedORB({"max_triggers_per_dir": 2, "retrigger_cooldown_sec": 0.0, "min_rvol": 0.0},
                            regime_filter=FakeRegime("NEUTRAL", 55.0))
    inst = "NIFTY"
    base = datetime(2026, 9, 29, 10, 0, 0, tzinfo=IST).timestamp()
    strat._reset_trigger_counts_if_new_day(inst, datetime(2026, 9, 29).date(), base)
    # base cooldown between identical strike triggers
    strat.cooldown_sec = 0

    fires = 0
    for i in range(20):
        ts = base + i * 300  # every 5 minutes, like the incident
        _prime_pending_pe(strat, inst)
        tick = {"token": "73907", "ltp": 130.0 - i, "volume": 5000.0, "exchange_timestamp": ts}
        strat.opt_5m_volumes["73907"] = deque([1000.0, 1000.0, 1000.0], maxlen=20)
        sig = await strat.on_tick(tick, meta=_opt_meta())
        if sig:
            fires += 1

    assert fires <= 2, f"Re-entry cap failed: ORB fired {fires} times (expected <= 2)"


@pytest.mark.asyncio
async def test_orb_regime_gate_blocks_counter_trend():
    """PE breakout must be blocked when regime is TRENDING_BULL (counter-trend)."""
    regime = FakeRegime("TRENDING_BULL", 75.0)
    strat = VolumeBackedORB({"max_triggers_per_dir": 5, "retrigger_cooldown_sec": 0.0, "min_rvol": 0.0},
                            regime_filter=regime)
    inst = "NIFTY"
    base = datetime(2026, 9, 29, 10, 0, 0, tzinfo=IST).timestamp()
    strat._reset_trigger_counts_if_new_day(inst, datetime(2026, 9, 29).date(), base)
    strat.cooldown_sec = 0
    _prime_pending_pe(strat, inst)
    strat.opt_5m_volumes["73907"] = __import__("collections").deque([1000.0, 1000.0, 1000.0], maxlen=20)

    sig = await strat.on_tick({"token": "73907", "ltp": 130.0, "volume": 5000.0,
                               "exchange_timestamp": base}, meta=_opt_meta())
    assert sig is None, "PE breakout should be blocked in TRENDING_BULL regime"


@pytest.mark.asyncio
async def test_orb_regime_gate_blocks_choppy():
    """All breakouts blocked when regime is CHOPPY."""
    regime = FakeRegime("CHOPPY", 38.0)
    strat = VolumeBackedORB({"max_triggers_per_dir": 5, "retrigger_cooldown_sec": 0.0, "min_rvol": 0.0},
                            regime_filter=regime)
    inst = "NIFTY"
    base = datetime(2026, 9, 29, 10, 0, 0, tzinfo=IST).timestamp()
    strat._reset_trigger_counts_if_new_day(inst, datetime(2026, 9, 29).date(), base)
    strat.cooldown_sec = 0
    _prime_pending_pe(strat, inst)
    strat.opt_5m_volumes["73907"] = __import__("collections").deque([1000.0, 1000.0, 1000.0], maxlen=20)

    sig = await strat.on_tick({"token": "73907", "ltp": 130.0, "volume": 5000.0,
                               "exchange_timestamp": base}, meta=_opt_meta())
    assert sig is None, "Breakout should be blocked in CHOPPY regime"


@pytest.mark.asyncio
async def test_orb_regime_gate_allows_aligned_trend():
    """PE breakout allowed when regime is TRENDING_BEAR (aligned)."""
    regime = FakeRegime("TRENDING_BEAR", 72.0)
    strat = VolumeBackedORB({"max_triggers_per_dir": 5, "retrigger_cooldown_sec": 0.0, "min_rvol": 0.0},
                            regime_filter=regime)
    inst = "NIFTY"
    base = datetime(2026, 9, 29, 10, 0, 0, tzinfo=IST).timestamp()
    strat._reset_trigger_counts_if_new_day(inst, datetime(2026, 9, 29).date(), base)
    strat.cooldown_sec = 0
    _prime_pending_pe(strat, inst)
    strat.opt_5m_volumes["73907"] = __import__("collections").deque([1000.0, 1000.0, 1000.0], maxlen=20)

    sig = await strat.on_tick({"token": "73907", "ltp": 130.0, "volume": 5000.0,
                               "exchange_timestamp": base}, meta=_opt_meta())
    assert sig is not None, "PE breakout should be allowed in aligned TRENDING_BEAR regime"
    assert sig["direction"] == "PE"
