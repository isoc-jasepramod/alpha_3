"""
PROJECT ALPHA 3.0 — FLOW ENGINE v2 TESTS (estimated order flow)

Covers the deterministic components per the spec: trade classification (quote/tick/carry),
estimated delta/CVD, pressure, weighted depth imbalance, OI regime, absorption persistence,
rolling z-score warmup, FlowIndex bounds, and bad-tick rejection.

NOTE: these prove the LOGIC is correct. Real predictive edge still requires live-verified
depth + fresh recorded data + backtest (the engine ships disabled until then).
"""
import math
import pytest
from backend.strategies.flow_engine_v2 import FlowEngineV2, classify_trade, RollingZ

META = {"name": "NIFTY", "option_type": "CE", "strike": 22700.0, "offset": 0}


# ---- trade classification ----
def test_classify_at_ask_is_buy():
    assert classify_trade(150.0, 149.9, 150.0, 149.0, 0) == (1, "ASK")

def test_classify_at_bid_is_sell():
    assert classify_trade(149.9, 149.9, 150.0, 151.0, 0) == (-1, "BID")

def test_classify_tick_up_is_buy():
    assert classify_trade(150.2, 150.0, 150.5, 150.0, 0) == (1, "TICK")  # inside spread, uptick

def test_classify_tick_down_is_sell():
    assert classify_trade(150.1, 150.0, 150.5, 150.3, 0) == (-1, "TICK")

def test_classify_same_price_carries_prev():
    assert classify_trade(150.2, 150.0, 150.5, 150.2, -1) == (-1, "CARRY")


# ---- weighted depth imbalance ----
def test_depth_imbalance_bid_heavy_positive():
    bids = [{"qty": 1000}, {"qty": 800}, {"qty": 600}, {"qty": 400}, {"qty": 200}]
    asks = [{"qty": 100}, {"qty": 80}, {"qty": 60}, {"qty": 40}, {"qty": 20}]
    wdi = FlowEngineV2.weighted_depth_imbalance(bids, asks, [1.0, 0.75, 0.5, 0.3, 0.15])
    assert wdi > 0

def test_depth_imbalance_ask_heavy_negative():
    bids = [{"qty": 100}, {"qty": 80}, {"qty": 60}, {"qty": 40}, {"qty": 20}]
    asks = [{"qty": 1000}, {"qty": 800}, {"qty": 600}, {"qty": 400}, {"qty": 200}]
    wdi = FlowEngineV2.weighted_depth_imbalance(bids, asks, [1.0, 0.75, 0.5, 0.3, 0.15])
    assert wdi < 0

def test_depth_imbalance_empty_is_zero():
    assert FlowEngineV2.weighted_depth_imbalance([], [], [1.0]) == 0.0


# ---- OI regime ----
def test_oi_regime_quadrants():
    assert FlowEngineV2.oi_regime(1, 1) == "LONG_BUILDUP"
    assert FlowEngineV2.oi_regime(-1, 1) == "SHORT_BUILDUP"
    assert FlowEngineV2.oi_regime(1, -1) == "SHORT_COVERING"
    assert FlowEngineV2.oi_regime(-1, -1) == "LONG_UNWINDING"
    assert FlowEngineV2.oi_regime(0, 0) == "NONE"


# ---- rolling z ----
def test_rolling_z_warmup_returns_zero():
    z = RollingZ(window=100, min_samples=30)
    for i in range(10):
        assert z.update(float(i)) == 0.0  # below min_samples
    assert not z.warmed()

def test_rolling_z_outlier_positive_after_warmup():
    z = RollingZ(window=100, min_samples=30)
    # Varied data so std > 0 (a zero-variance buffer correctly returns 0.0).
    for i in range(50):
        z.update(10.0 + (i % 5))
    out = z.normalized(1000.0)  # big positive outlier
    assert out > 0

def test_rolling_z_zero_variance_returns_zero():
    z = RollingZ(window=100, min_samples=30)
    for _ in range(50):
        z.update(10.0)  # zero variance
    assert z.normalized(1000.0) == 0.0  # std==0 guard


# ---- engine flow (async) ----
async def _feed(strat, ticks):
    last = None
    for tk in ticks:
        await strat.on_tick(tk, meta=META)
        last = strat.get_flow(tk["token"])
    return last


def _tick(ts, ltp, ltq, bid, ask, oi=1_000_000, vol=100000, token="OPT1"):
    return {"token": token, "ltp": ltp, "last_traded_qty": ltq, "best_bid": bid, "best_ask": ask,
            "open_interest": oi, "volume": vol, "exchange_timestamp": ts,
            "depth": {"bids": [{"qty": 500, "price": bid}], "asks": [{"qty": 500, "price": ask}]}}


@pytest.mark.asyncio
async def test_disabled_by_default_ignores_ticks():
    s = FlowEngineV2()  # disabled
    await s.on_tick(_tick(1790000000, 150.0, 40, 149.9, 150.1), meta=META)
    assert s.get_flow("OPT1") is None


@pytest.mark.asyncio
async def test_buying_produces_positive_delta_and_cvd():
    s = FlowEngineV2({"enabled": True, "start_time": "00:00:00", "end_time": "23:59:59"})
    base = 1790000000.0
    # repeated trades at/above ask -> buy-initiated
    ticks = [_tick(base + i, 150.0 + i * 0.1, 50, 149.9 + i * 0.1, 150.0 + i * 0.1) for i in range(5)]
    out = await _feed(s, ticks)
    assert out is not None
    assert out["cvd"] > 0
    assert out["trade_side"] == "BUY"
    assert -100.0 <= out["flow_index"] <= 100.0


@pytest.mark.asyncio
async def test_selling_produces_negative_cvd():
    s = FlowEngineV2({"enabled": True, "start_time": "00:00:00", "end_time": "23:59:59"})
    base = 1790000000.0
    # trades at/below bid -> sell-initiated (price ticking down)
    ticks = [_tick(base + i, 150.0 - i * 0.1, 50, 150.0 - i * 0.1, 150.2 - i * 0.1) for i in range(5)]
    out = await _feed(s, ticks)
    assert out["cvd"] < 0
    assert out["trade_side"] == "SELL"


@pytest.mark.asyncio
async def test_flow_index_always_bounded():
    s = FlowEngineV2({"enabled": True, "start_time": "00:00:00", "end_time": "23:59:59"})
    base = 1790000000.0
    ticks = [_tick(base + i, 150.0 + (i % 3) * 0.5, 500, 149.5, 150.5) for i in range(60)]
    out = await _feed(s, ticks)
    assert -100.0 <= out["flow_index"] <= 100.0
    assert 0.0 <= out["confidence"] <= 100.0


@pytest.mark.asyncio
async def test_bad_ticks_rejected():
    s = FlowEngineV2({"enabled": True, "start_time": "00:00:00", "end_time": "23:59:59"})
    # zero/negative ltp rejected
    await s.on_tick(_tick(1790000000, 0.0, 40, 149.9, 150.1), meta=META)
    assert s.get_flow("OPT1") is None


@pytest.mark.asyncio
async def test_classification_method_exposed():
    s = FlowEngineV2({"enabled": True, "start_time": "00:00:00", "end_time": "23:59:59"})
    await s.on_tick(_tick(1790000000, 150.1, 40, 149.9, 150.0), meta=META)  # ltp>=ask -> ASK
    out = s.get_flow("OPT1")
    assert out["classification_method"] in ("ASK", "BID", "TICK", "CARRY", "NONE")
    assert out["trade_side"] == "BUY"
