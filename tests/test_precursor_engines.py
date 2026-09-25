import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import pytest
from datetime import datetime, timezone
from backend.strategies.iv_engine import IVEngine, solve_iv, bs_price_and_greeks
from backend.strategies.gex_engine import GEXEngine
from backend.strategies.flow_engine import FlowEngine
from backend.strategies.squeeze_detector import SqueezeDetector
from backend.strategies.oi_squeeze import OISqueezeSentinel

def test_iv_solver():
    # Spot 24000, Strike 24000 (ATM Call), 5 days to expiry, 20% vol -> theoretical price ~196
    spot = 24000.0
    strike = 24000.0
    t_years = 5.0 / 365.0
    theo_price, delta, gamma = bs_price_and_greeks(spot, strike, t_years, 0.20, 0.07, "CE")
    assert theo_price > 100.0
    assert 0.45 <= delta <= 0.60
    assert gamma > 0.0

    solved = solve_iv(theo_price, spot, strike, t_years, 0.07, "CE")
    assert solved is not None
    assert abs(solved - 0.20) < 0.02

@pytest.mark.asyncio
async def test_gex_engine():
    iv_eng = IVEngine()
    gex_eng = GEXEngine(iv_engine=iv_eng)
    gex_eng.update_spot("NIFTY", 24000.0, 1727260000.0)

    # Feed Call and Put ticks
    await gex_eng.on_tick(
        {"token": "1001", "ltp": 150.0, "open_interest": 5000000, "exchange_timestamp": 1727260001.0},
        {"name": "NIFTY", "option_type": "CE", "strike": 24100.0, "lot_size": 50}
    )
    await gex_eng.on_tick(
        {"token": "1002", "ltp": 120.0, "open_interest": 4000000, "exchange_timestamp": 1727260002.0},
        {"name": "NIFTY", "option_type": "PE", "strike": 23900.0, "lot_size": 50}
    )

    assert 24100.0 in gex_eng.chain_data["NIFTY"]
    assert "CE" in gex_eng.chain_data["NIFTY"][24100.0]

@pytest.mark.asyncio
async def test_flow_engine_absorption():
    flow = FlowEngine()
    # 1727241000 is 2024-09-25 10:40:00 IST (in-session)
    base_ts = 1727241000.0
    flow.update_spot("NIFTY", 24000.0, base_ts)

    # Initial tick
    await flow.on_tick(
        {"token": "1001", "ltp": 100.0, "volume": 10000, "total_buy_qty": 50000, "total_sell_qty": 20000, "exchange_timestamp": base_ts},
        {"name": "NIFTY", "offset": 0, "option_type": "CE"}
    )
    # Subsequent tick with aggressive buy volume surge
    await flow.on_tick(
        {"token": "1001", "ltp": 100.5, "volume": 120000, "total_buy_qty": 150000, "total_sell_qty": 30000, "exchange_timestamp": base_ts + 100.0},
        {"name": "NIFTY", "offset": 0, "option_type": "CE"}
    )

    data = flow.token_flow.get("1001")
    assert data is not None
    assert data["cvd"] > 0

@pytest.mark.asyncio
async def test_oi_squeeze_strike_expansion():
    sentinel = OISqueezeSentinel()
    base_ts = 1727241000.0
    sentinel.update_spot("NIFTY", 24000.0, base_ts)
    sentinel.spot_ema20["NIFTY"].seed(23950.0)

    # Base tick for offset = -1 (near-ATM strike that was previously rejected)
    res1 = await sentinel.on_tick(
        {"token": "73904", "ltp": 140.0, "open_interest": 8000000, "volume": 10000, "exchange_timestamp": base_ts},
        {"name": "NIFTY", "offset": -1, "option_type": "CE", "strike": 23950.0, "lot_size": 50, "symbol": "NIFTY23950CE"}
    )
    assert res1 is None # baseline accumulated

    # Squeeze tick: -35% OI, +40% Price
    res2 = await sentinel.on_tick(
        {"token": "73904", "ltp": 196.0, "open_interest": 5200000, "volume": 80000, "exchange_timestamp": base_ts + 150.0},
        {"name": "NIFTY", "offset": -1, "option_type": "CE", "strike": 23950.0, "lot_size": 50, "symbol": "NIFTY23950CE"}
    )
    # Should NOT be rejected due to offset != 0
    assert len(sentinel.token_history["73904"]) == 2

@pytest.mark.asyncio
async def test_oi_squeeze_early_ignition():
    sentinel = OISqueezeSentinel()
    base_ts = 1727241000.0  # 10:40 IST (active trading window)
    sentinel.update_spot("NIFTY", 24000.0, base_ts)
    sentinel.spot_ema20["NIFTY"].seed(23980.0)

    # 0-DTE option metadata
    meta = {
        "name": "NIFTY",
        "offset": 0,
        "option_type": "CE",
        "strike": 24000.0,
        "lot_size": 50,
        "symbol": "NIFTY24000CE",
        "expiry": "25Sep2024"
    }

    # 4 warm-up ticks establishing baseline
    for i in range(4):
        await sentinel.on_tick(
            {"token": "73904", "ltp": 100.0, "open_interest": 10000000, "volume": 10000, "exchange_timestamp": base_ts + (i * 12.0)},
            meta
        )

    # Early Ignition tick: +9.0% price surge, -2.5% OI unwinding, 50s total span
    sig = await sentinel.on_tick(
        {"token": "73904", "ltp": 109.0, "open_interest": 9750000, "volume": 15000, "total_buy_qty": 60000, "total_sell_qty": 20000, "exchange_timestamp": base_ts + 50.0},
        meta
    )

    assert sig is not None
    assert sig["details"]["squeeze_type"] == "EARLY_IGNITION"
    assert sig["entry_price"] == 109.0
    assert sig["direction"] == "CE"

@pytest.mark.asyncio
async def test_confluence_pre_entry_alert():
    iv_eng = IVEngine()
    gex_eng = GEXEngine(iv_engine=iv_eng)
    base_ts = 1727241000.0
    gex_eng.update_spot("NIFTY", 24080.0, base_ts)

    # Seed IV Skew history to simulate +2.5% Call wing skew surge
    iv_eng.skew_history["NIFTY"].append((base_ts - 120.0, 1.0))
    iv_eng.skew_history["NIFTY"].append((base_ts, 3.5))

    # Add strikes around 24100 (Call Wall)
    for idx, k in enumerate([24000.0, 24050.0, 24100.0, 24150.0]):
        ts = base_ts + idx * 2.0
        await gex_eng.on_tick(
            {"token": f"CE_{int(k)}", "ltp": 120.0, "open_interest": 8000000 if k == 24100.0 else 2000000, "exchange_timestamp": ts},
            {"name": "NIFTY", "option_type": "CE", "strike": k, "lot_size": 50}
        )
        await gex_eng.on_tick(
            {"token": f"PE_{int(k)}", "ltp": 80.0, "open_interest": 2000000, "exchange_timestamp": ts},
            {"name": "NIFTY", "option_type": "PE", "strike": k, "lot_size": 50}
        )

    # Final tick to trigger GEX regime evaluation (>5s elapsed)
    await gex_eng.on_tick(
        {"token": "CE_24100", "ltp": 125.0, "open_interest": 8000000, "exchange_timestamp": base_ts + 15.0},
        {"name": "NIFTY", "option_type": "CE", "strike": 24100.0, "lot_size": 50}
    )

    # Check that elevated CONFLUENCE_PRE_ENTRY alert fired
    alerts = list(gex_eng.pending_alerts)
    confluence_alerts = [a for a in alerts if a.get("alert_type") == "CONFLUENCE_PRE_ENTRY"]
    assert len(confluence_alerts) > 0
    al = confluence_alerts[0]
    assert al["direction"] == "CE"
    assert al["details"]["entry_strike"] == 24100.0
    assert al["details"]["recommended_sl"] == 24080.0 - 25.0
    assert al["details"]["target_1"] == 24100.0 + 35.0
