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
