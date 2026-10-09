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

---

## Live A/B result — 2026-10-09 (first live session) + recalibration

The recorder ran a full live session: 133 NIFTY / 129 SENSEX candles, each with v1, v2, and a
live breadth snapshot.

**Operational:** breadth poller 100% valid all session; v2 was **not** jumpier live (transitions
v1=32/v2=31 NIFTY, v1=33/v2=34 SENSEX — the backtest whipsaw fear did not materialize);
trending-agreement 84% NIFTY, 90% SENSEX.

**Critical flaw found — v2 was systematically too conservative, and wrong when it mattered.**
Of the v1-vs-v2 disagreements, v2 demoted a trend v1 called TRENDING to NEUTRAL on 18 (NIFTY) /
11 (SENSEX) candles — vs only 3/2 the other way. These demotions happened when DI+ dominated,
breadth was 0.7–0.9 bullish, and weighted pressure was +1.4 to +1.7% — i.e. v2 was most cautious
exactly when the trend was healthiest and broadest. On a +₹19k trend-up day, had v2 been gating it
would have suppressed real winners.

Root cause (diagnosed, not guessed):
1. **Efficiency Ratio over-penalized.** 14/18 NIFTY demotions had ER < 0.25 while ADX ≥ 25. ER
   collapses whenever a trend pauses mid-move, and at weight 0.30 it alone subtracts ~27 pts from
   trend_quality — re-coupling the exact variables the v2 redesign set out to decouple.
2. **Thresholds inherited from v1 didn't fit v2's score distribution.** Demoted candles clustered
   at score 61–62, just under the 65 line.

### Recalibration (config "F", `lab_services/calibrate_regime_v2.py`)

Swept weight/threshold configs across Oct 6/7/8/9 (Oct 9 with recorded live breadth), scored on
trend-capture (strong moves labelled TRENDING-correct) vs chop-rejection (TRENDING calls that
followed through) + Oct-7 false-trend rate + recovery of the Oct-9 demotions.

Chosen: **weight_er 0.30→0.18, weight_adx 0.30→0.35, weight_persistence 0.25→0.27,
weight_slope 0.15→0.20, trending_threshold 65→60, neutral_threshold 45→43.**
(The "DI+breadth override" idea was tested and **dropped** — once ER weight and threshold were
fixed, the override never bound; it was dead complexity.)

| metric | current | config F |
|---|---|---|
| trend-capture (strong moves labelled TRENDING-correct) | 14.2% | 26.8% |
| chop-rejection (TRENDING follow-through hit-rate) | 50.8% | 54.7% |
| Oct-7 chop-day bars wrongly TRENDING | 14.8% | 23.4% |
| Oct-9 healthy-trend demotions recovered | 51.7% | 89.7% |

Verified end-to-end: the live `RegimeFilterV2` loaded from `market_rules.yaml` recovers 26/29 (90%)
of the Oct-9 demotions.

**Honest limits:** config F roughly doubles trend-capture and improves chop-rejection, but still
only captures ~27% of strong moves and mislabels ~23% of the chop day. This is "no longer broken in
the way we found," not "solved." Forward-move is a crude proxy and 4 days is a small sample. v2 stays
**non-gating** and under live observation. The live A/B did its job: it caught a calibration flaw
*before* v2 ever gated a trade.

### What the first live A/B answered (vs the three questions above)
1. Chop rejection: not yet tested live (Oct 9 was a trend day). Pending a live chop session.
2. Breadth divergence vs exhaustion: inconclusive — breadth stayed strongly bullish through the
   afternoon give-back, so breadth did NOT flag the exhaustion. Reinforces that exhaustion is a
   separate problem (velocity/maturity), not a breadth signal. See exhaustion-damper plan.
3. Stability: answered — v2 is stable live (transitions ≈ v1), not whippy.
