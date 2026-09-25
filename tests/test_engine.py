import pytest
import os
from backend.strategies.indicators import (
    IncrementalEMA,
    IncrementalVWAP,
    IncrementalRSI,
    IncrementalATR,
    calculate_bottom_wick_ratio,
    calculate_top_wick_ratio
)
from backend.risk.risk_governor import RiskGovernor
from lab_services.replay_engine import MarketReplayEngine
from lab_services.spot_backtester import SpotHistoricalBacktester

def test_indicators_math():
    # EMA test
    ema = IncrementalEMA(period=5)
    prices = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
    for p in prices:
        val = ema.update(p)
    assert ema.value is not None
    assert 12.0 < ema.value < 15.0

    # VWAP test
    vwap = IncrementalVWAP()
    vwap.update(100.0, 10)
    vwap.update(110.0, 20)
    expected_vwap = (100 * 10 + 110 * 20) / 30 # 3200 / 30 = 106.666...
    assert round(vwap.value, 2) == 106.67

    # Wick ratios test
    # Candle: Open 100, High 110, Low 90, Close 105
    # Bottom wick = (min(100, 105) - 90) / (110 - 90) = 10 / 20 = 0.50
    b_wick = calculate_bottom_wick_ratio(100.0, 110.0, 90.0, 105.0)
    assert b_wick == 0.50

    # Top wick = (110 - max(100, 105)) / 20 = 5 / 20 = 0.25
    t_wick = calculate_top_wick_ratio(100.0, 110.0, 90.0, 105.0)
    assert t_wick == 0.25

    # Doji zero-division protection
    assert calculate_bottom_wick_ratio(100.0, 100.0, 100.0, 100.0) == 0.0

def test_risk_governor_math():
    gov = RiskGovernor()
    gov.total_equity = 100000.0
    gov.update_spot_bar("NIFTY", 25100.0, 24900.0, 25000.0)

    raw_sig = {
        "signal_id": "TEST-SIG-1",
        "instrument": "NIFTY",
        "direction": "CE",
        "option_type": "CE",
        "spot_entry": 25000.0,
        "entry_price": 100.0,
        "lot_size": 65
    }

    evaluated = gov.evaluate_signal(raw_sig)
    assert evaluated is not None
    assert evaluated["entry_price"] == 100.0
    # Stop loss floor rule: If SL < 0.75 * 100 (75), capped at 0.80 * 100 (80)
    assert evaluated["stop_loss"] >= 75.0
    # Target must be 1:2 RR
    risk = evaluated["entry_price"] - evaluated["stop_loss"]
    assert round(evaluated["target"], 2) == round(evaluated["entry_price"] + 2 * risk, 2)
    # Quantity must be multiple of lot_size
    assert evaluated["quantity"] % 65 == 0

    # Circuit breaker test
    gov.record_trade_result(-2600.0) # -2.6% on 1,00,000 equity (limit is -2.5% = -2500)
    assert gov.circuit_breaker_tripped is True
    # Circuit breaker is tripped, but signal emitted for advisory tracking
    advisory_sig = gov.evaluate_signal(raw_sig)
    assert advisory_sig is not None


def test_replay_sample_generation_and_duckdb(tmp_path):
    replayer = MarketReplayEngine(data_lake_dir=str(tmp_path))
    sample_file = replayer.check_or_create_sample_data("2026-09-20")
    assert os.path.exists(sample_file)

def test_backtester_metrics():
    backtester = SpotHistoricalBacktester()
    df = backtester.generate_synthetic_historical_data(days=5)
    assert len(df) > 0
    res = backtester.backtest_orb(df)
    assert "win_rate_pct" in res
    assert "total_trades" in res
