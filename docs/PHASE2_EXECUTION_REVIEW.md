# Phase 2 Live Execution — Review & Punch-List

**Scope:** Review of commit `81fbd9a` ("Phase 2 Live broker execution, limit order safeguards, and rejection handling"), which converted the execution bridge from paper-only (hard-locked) to placing **real AngelOne broker orders**.

**Status at review:** 78 tests pass. Live path works against a mocked broker. The concerns below are about behaviour with a *real* broker, which the tests do not exercise.

**Verdict:** The skeleton is solid and several parts are well-built. But as committed, the system trades real money by default on startup, marks unfilled limit orders as "filled" at a price it never paid, and does not implement the limit-order lifecycle it advertises. Do **not** route real capital until the P0 items below are resolved.

---

## What's well done (keep as-is)

- **Exit-failure preserves the position.** `handle_full_exit` keeps a position in `active_positions` with `exit_failed=True` + CRITICAL log + Telegram alert when a SELL is rejected, instead of dropping it. Correct — never lose track of real exposure.
- **Broker RMS rejection path.** Rejection → audit record + Redis `EXECUTION_ORDER_REJECTED` + no phantom position created.
- **Tick-size alignment** to 0.05 (`align_tick_size`), with zero/negative guards. Required for NSE/BSE options.
- **Auth re-login attempt** before failing; clean `REJECTED_BROKER_UNAUTHENTICATED` guard.
- **Idempotency** (`signal_to_order`), capital pool, circuit breaker, lot-multiple guard — all gate live orders.

---

## P0 — Blockers before any real capital

### P0-1. LIVE is the committed default; micro-lot safety is OFF
- `backend/api/server.py:56` — `AngelExecutionBridge(..., force_dry_run=False)`.
- `config/settings.yaml` execution block — `mode: LIVE`, `dry_run: false`, `enabled: true`, `kill_switch_active: false`, `force_single_lot: false`.
- **Effect:** On server start with a valid broker session, the system places **real orders up to the full ₹25k pool across 2 positions**, with no arming step or confirmation. A routine restart goes straight to live.
- **Fix:** Ship safe defaults — `mode: PAPER`, `dry_run: true`, `force_single_lot: true`. Going LIVE must be an explicit, deliberate operator action (the `/api/execution/mode` endpoint already exists), never a boot state. Consider requiring both `enabled=true` AND an explicit arm toggle before live routing.

### P0-2. Position marked FILLED at a fictional price
- `backend/execution/angel_execution_bridge.py:560` — after `placeOrder` returns, `fill_price = metadata["expected_entry"]` and the record is written `status: "FILLED"`.
- **Why it's wrong:** A returned order ID means **ACCEPTED, not FILLED**. A LIMIT order can sit open and unfilled. Even when filled, the real price comes from the order/trade book, not the signal's expected entry.
- **Consequences:**
  - If the limit never fills, the system believes it holds a position it doesn't — and later places a real SELL to "exit" it, creating a **naked short**.
  - P&L, breakeven SL, deployed capital, and the circuit breaker are all computed off a price never paid.
  - Defeats the purpose of going live (records theoretical *as* realized).
- **Fix:** Do not mark FILLED on acceptance. Record `ACCEPTED/OPEN`, then confirm via the broker (see fill-confirmation flow below) and set `fill_price` + `FILLED/PARTIAL` from the actual trade.

### P0-3. Limit-order lifecycle is configured but not implemented
- `backend/execution/angel_execution_bridge.py:142-143` — `limit_timeout_sec` (15) and `retry_on_network_failure` are loaded and persisted, but **never referenced in execution logic** (verified: they appear only in config plumbing + the UI config route).
- No price-chase, no convert-to-market, no abort/cancel-after-timeout, no `cancelOrder` anywhere.
- Buffer is still **0.5%** (`slippage_buffer_pct: 0.5`) — too tight for options (industry norm is multiples of this). Combined with P0-2, the system places a tight limit and then *assumes* it filled.
- **Fix:** Implement the lifecycle: place limit → poll fill status until `limit_timeout_sec` → if unfilled, **cancel and abort** (and record an aborted signal) or chase per an explicit, tested policy. Re-tune the buffer for options against paper fills.

### P0-4. No fill confirmation / no reconciliation (root cause of P0-2 & P0-3)
- No `getOrderBook` / order-status polling and no order-update websocket — the system cannot learn an order's true state.
- `_load_today_orders` rebuilds state purely from the local JSON audit file on restart; it never queries the broker's positionbook. A crash while holding a real position = assumed state can silently diverge from reality.
- **Fix:** Add order-status confirmation (poll `getOrderBook`/order-status, or subscribe to the order-update feed) and a startup reconciliation that trusts the broker positionbook/orderbook as source of truth, correcting local state.

---

## P1 — Important, fix before scaling beyond micro pilot

- **P1-1. Partial fills unhandled.** `remaining_quantity` is set to full qty regardless of actual fill (`angel_execution_bridge.py:560+`). A partial fill mis-states position and the scale-and-trail math.
- **P1-2. Exit price is also assumed.** `handle_partial_exit`/`handle_full_exit` compute realized P&L from the passed-in `exit_price` (the target/stop level), not the actual SELL fill. Same fiction as P0-2 on the way out.
- **P1-3. No DB orders table.** Order state lives in memory + daily JSON only. For real-money auditing and crash recovery, persist orders/fills to the DB (schema currently has only `Signal`/`DailyJournal`).
- **P1-4. Test fixture masks the live default.** `tests/test_execution_bridge.py` fixture forces `force_dry_run=True`, so `test_api_mode_toggle_endpoint` asserts "default mode == PAPER" — not production's actual `force_dry_run=False` / `mode: LIVE`. Live-path tests mock `placeOrder` to return instantly, baking in the instant-fill assumption. Add a test asserting the *production* instantiation's default mode, and a test where the mocked order stays OPEN (unfilled) to prove the lifecycle handles it.

---

## Fill-confirmation flow (the linchpin — sketch)

Replace "placeOrder returns → mark FILLED at expected_entry" with a confirmation loop:

```
place_entry(signal):
    resp = smart_connect.placeOrder(limit_payload)   # BUY LIMIT at buffered price
    order_id = parse(resp)                            # reject/exception handling as today
    record order: status = ACCEPTED, fill_price = None, placed_at = now

    deadline = now + limit_timeout_sec
    loop until deadline:
        st = smart_connect.getOrderBook()/order-status(order_id)   # run_in_executor
        if st.status == COMPLETE:
            fill_price = st.averageprice            # REAL fill
            filled_qty = st.filledshares
            record: status = FILLED (or PARTIAL if filled_qty < qty), fill_price
            open position with REAL fill_price; anchor SL/target to fill_price
            return FILLED
        if st.status in (REJECTED, CANCELLED):
            record rejection; no position; return REJECTED
        sleep(poll_interval)   # e.g. 1s

    # timed out, still OPEN/partial
    smart_connect.cancelOrder(order_id)
    if filled_qty == 0:
        record ABORTED_UNFILLED; no position; return ABORTED
    else:
        keep the partial as the position at its real fill_price; log partial
```

Key points:
- **Anchor stops/targets to the real fill price**, not the signal/expected price (ties to the earlier discussion — worse fill + fixed stop degrades R:R).
- **Record aborts** (unfilled signals) so realized-vs-theoretical can be measured — aborts skew toward the fast winners, so track the winner-miss rate.
- Exits need the same treatment: confirm the SELL fill before marking the position closed / booking P&L; keep the existing exit-failure preservation on top.

---

## Suggested sequencing

1. **Flip defaults to safe (P0-1)** — one-line + config; immediately removes the "live on boot" risk.
2. **Fill confirmation (P0-2 + P0-4)** — the linchpin; unblocks correct P&L, partial fills, and the limit lifecycle.
3. **Limit lifecycle + buffer tuning (P0-3)** — build on the confirmation loop; validate buffer against paper fills (a backtest cannot model fill latency).
4. **P1 items** before moving off the single-lot micro pilot.

Measure in paper mode with the confirmation flow active for a few sessions, comparing **actual fills and aborts** to the theoretical signals, before arming live capital.
