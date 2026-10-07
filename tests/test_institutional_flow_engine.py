import pytest
from backend.strategies.institutional_flow_engine import InstitutionalFlowEngine, RollingMetric

def test_rolling_metric():
    rm = RollingMetric(window_sec=60.0)
    rm.add(100.0, 500.0)
    rm.add(120.0, 300.0)
    assert rm.sum(130.0) == 800.0

    # Eviction past 60s
    rm.add(170.0, 200.0) # 100.0 should be evicted at t=170
    assert rm.sum(170.0) == 500.0 # 300 + 200


def test_institutional_flow_engine_ptv():
    engine = InstitutionalFlowEngine(config={"window_sec": 60.0})

    # Default state is Neutral
    st = engine.get_flow_state("NIFTY")
    assert st["direction"] == "NEUTRAL"
    assert st["aifi"] == 0.0

    meta_ce = {"name": "NIFTY", "option_type": "CE", "is_spot": False}
    meta_pe = {"name": "NIFTY", "option_type": "PE", "is_spot": False}

    base_ts = 1000.0
    # Tick 1: baseline
    engine.on_tick({"token": "101", "ltp": 100.0, "volume": 1000, "open_interest": 50000, "exchange_timestamp": base_ts}, meta_ce)
    engine.on_tick({"token": "102", "ltp": 100.0, "volume": 1000, "open_interest": 50000, "exchange_timestamp": base_ts}, meta_pe)

    # Tick 2: Heavy Call Buying (+50,000 volume in CE vs only +1,000 in PE)
    res_ce = engine.on_tick({
        "token": "101",
        "ltp": 110.0,
        "volume": 51000, # +50k vol = 50k * 110 = ₹55 Lakhs turnover
        "open_interest": 48000, # -2k OI unwind!
        "exchange_timestamp": base_ts + 10.0,
        "bid1_qty": 20000, "ask1_qty": 5000
    }, meta_ce)

    assert res_ce is not None
    assert res_ce["ptv_score"] > 0.5 # Heavy Call turnover dominance
    assert res_ce["direction"] == "BULLISH"
    assert res_ce["aifi"] > 0.3


def test_institutional_flow_engine_depth_obi():
    engine = InstitutionalFlowEngine()
    meta_pe = {"name": "SENSEX", "option_type": "PE", "is_spot": False}

    # Tick with heavy bid-side depth
    tick = {
        "token": "201",
        "ltp": 250.0,
        "volume": 5000,
        "open_interest": 20000,
        "exchange_timestamp": 1000.0,
        "depth": {
            "bids": [{"qty": 5000}, {"qty": 3000}],
            "asks": [{"qty": 1000}, {"qty": 500}]
        }
    }
    obi = engine._compute_obi(tick)
    assert obi > 0.5 # Strongly bid-heavy
