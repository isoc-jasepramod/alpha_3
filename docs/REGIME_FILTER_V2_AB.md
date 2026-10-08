# Regime Filter v2 — A/B Candidate (non-gating)

Status: **SHIPPED NON-GATING.** v2 computes in parallel with the live v1 filter and is
recorded per candle, but **no strategy consumes it.** Live strategy behavior
(ORB / VWAP_EMA / Momentum / OI Squeeze) is unchanged.

## What v2 changes vs v1

Trend quality (0–100 score), direction decoupled from strength, 6 states:

- **ADX** strength with rising-ADX bonus (as v1).
- **Efficiency Ratio** (Kaufman) — clean trend vs chop (as v1).
- **VWAP persistence** — now **distance-weighted** (how far from VWAP, not just which side) with a cross penalty.
- **VWAP slope** — now **ATR-normalized** (volatility-adjusted, comparable across days/instruments).
- **Direction** — decoupled: DI+/DI- primary, then price-vs-VWAP, slope sign, breadth sign (weighted vote).
- **Participation / market breadth** — NEW dimension from index heavyweights (`MarketBreadthEngine`).
  **Live-only** (polled REST); there is no recorded constituent history, so it cannot be backtested.
- States: `TRENDING_BULL` / `TRENDING_BEAR` × `HEALTHY` / `WEAKENING` (slope-fade), `NEUTRAL`, `CHOPPY`.

## Trend-quality backtest (breadth excluded)

`lab_services/backtest_regime_v2.py` replays recorded spot ticks through the same indicator
stack the live engine uses, feeds both filters, and resolves each regime label against forward
spot movement over 4 bars (~12 min), measured in ATR units. **Trend-quality only** —
`use_participation=False`. Follow-through is a directional proxy, **not** option-premium PnL.

Pooled over 2026-10-06 (trend-up), 2026-10-07 (chop), 2026-10-08 (trend-down → exhaustion):

| metric | NIFTY v1 | NIFTY v2 | SENSEX v1 | SENSEX v2 |
|---|---|---|---|---|
| TRENDING bars | 261 | 91 | 258 | 94 |
| TRENDING follow-through | 49.8% | 45.1% | 53.5% | 50.0% |
| regime transitions | 68 | 84 | 67 | 81 |

### Honest read

- **v2 is more selective** — it calls far fewer bars TRENDING (by design: distance-weighted
  persistence + ATR-normalized slope raise the bar).
- **v2's trend-quality portion is NOT a clear win.** Pooled follow-through is slightly *worse*
  than v1, and both filters sit around a coin-flip. v2 is also *jumpier* (more transitions),
  not more stable as hoped.
- **Per-day, the one place v2 helps is the chop day (Oct 7):** it dumps many bars into CHOPPY
  (strongly negative forward move) and its few TRENDING calls had higher follow-through than v1
  (NIFTY 45.5% vs 30.4%). That is exactly the Oct 7 failure mode we care about.
- **v2 hurts on the clean trend day (Oct 6):** too conservative, mislabels trend as NEUTRAL/CHOPPY,
  poor follow-through on its sparse TRENDING calls.
- **Neither filter detects the Oct 8 afternoon exhaustion** — consistent with the earlier finding
  that those losses were trend-exhaustion, not chop. v2 does not address exhaustion.

**Conclusion:** the trend-quality redesign alone does not justify gating. Its potential value is
(a) chop rejection and (b) the **breadth/participation dimension that cannot be backtested** — the
reason for the live A/B.

## Live A/B recording

`backend/core/regime_recorder.py` writes one JSONL row per spot-candle-close to
`data/regime_compare/date=YYYY-MM-DD/<INST>.jsonl`, capturing both filters' state + the breadth
snapshot v2 used. Enabled via `config/market_rules.yaml → strategies.regime_filter_v2`
(`enabled: true`, `record: true`). Breadth poller runs only when `use_participation: true`.

Next-day comparison questions:
1. On chop sessions, does v2 flag CHOPPY where v1 stayed TRENDING, and were those v1-TRENDING
   bars the ones that produced the losing signals?
2. Does breadth divergence (price up, breadth weak) precede the exhaustion give-backs v1 misses?
3. Is v2 stable enough live, or does the extra transition count translate into whipsaw?

Until these are answered on live data, v2 stays non-gating.
