# Alpha 3.0

> High-Frequency Algorithmic Trading & Precursor Prediction Engine for Indian Index Derivatives (NIFTY & SENSEX).

## Overview

Alpha 3.0 is an institutional-grade quantitative trading system designed for ultra-low latency detection of options market anomalies, dealer gamma squeezes, and pre-impulse order flow precursors.

### Core Architecture & Engines

1. **Precursor Predictive Intelligence:**
   - **IV Engine (`backend/strategies/iv_engine.py`):** Real-time Black-Scholes implied volatility back-solver tracking 25-Delta Risk Reversal ($RR = IV_{\text{Call}} - IV_{\text{Put}}$) and IV skew expansion velocity.
   - **GEX Engine (`backend/strategies/gex_engine.py`):** Calculates strike-level Net Dealer Gamma Exposure ($OI \times \Gamma \times S^2$), Dealer Zero-Gamma Flip level, and Call/Put Gamma Walls.
   - **Flow Engine (`backend/strategies/flow_engine.py`):** Detects Order Flow Imbalance (OFI) and Cumulative Volume Delta (CVD) absorption from live bid/ask queue data.
   - **Squeeze Detector (`backend/strategies/squeeze_detector.py`):** Identifies Bollinger Bands compressed inside Keltner Channels (John Carter compression) prior to multi-sigma explosions.
   - **Chain Poller (`backend/core/chain_poller.py`):** Aggregates chain-wide Put-Call Ratio (PCR) velocity, Max Pain migration, and multi-strike short covering cascades.

2. **Execution Strategies:**
   - **OI Squeeze Sentinel (`backend/strategies/oi_squeeze.py`):** Multi-strike rolling unwinding scanner ($dOI \le -5\%$, $dP \ge +3\%$) with dynamic warm-up protection.
   - **Expiry Day Gamma Scalp (`backend/strategies/gamma_scalp.py`):** Tick-level velocity breakouts from midday consolidation ranges.
   - **Volume Backed ORB (`backend/strategies/orb_breakout.py`):** Opening Range Breakout confirmed by institutional volume surges.
   - **VWAP EMA Alignment (`backend/strategies/vwap_ema.py`):** Multi-timeframe trend filter and pullback rejection detector.
   - **Momentum Impulse (`backend/strategies/momentum_impulse.py`):** High-speed tick impulse velocity detector.

3. **Data Ingestion & Core Infrastructure:**
   - **SmartAPI WebSocket 2.0 (`backend/core/websocket_client.py`):** Binary stream with 25s application-level keepalive and jittered exponential reconnect.
   - **Historical Candle Warm-up:** Seeds indicators from AngelOne REST on startup for zero cold-start lag.
   - **Redis Pub/Sub Bus (`backend/core/redis_bus.py`):** Sub-millisecond tick broadcast and radar pre-alert distribution.
   - **Master Risk Governor (`backend/risk/risk_governor.py`):** Capital protection, dynamic stop-loss, and multi-stage profit targets.

---

## Getting Started

### Prerequisites
- Python 3.11+
- Redis Server running on `localhost:6379`
- AngelOne SmartAPI Trading Account & API credentials

### Configuration
Create a `.env` file in the project root:
```env
ANGEL_API_KEY=your_api_key
ANGEL_CLIENT_CODE=your_client_code
ANGEL_PIN=your_mpin
ANGEL_TOTP_TOKEN=your_totp_secret
```

### Installation & Run
```bash
# Install dependencies
pip install -r requirements.txt

# Run Backend
python backend/main.py
```
