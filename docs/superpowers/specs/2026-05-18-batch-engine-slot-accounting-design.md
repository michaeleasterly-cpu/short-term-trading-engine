# Batch-Engine Slot Accounting (#251) — Design **v1.1 (expert-scoped; A+B in-scope)**

**Status:** spec **v1.1** 2026-05-18 (platform-overlay /
RiskGovernor). Scope-by-investigation → spec v1 → **operator review
gate: APPROVED with "also fix the dual-decrement now" (NOT deferred)**
→ focused expert design pass on Part B → **spec v1.1 (this rev — Part
B folded in-scope)** → plan → phased subagent build. TODO §"Governor
follow-ups" item #251. **Live-money risk control — the
never-fail-open invariant is sacred.**

**v1.1 change:** the operator decided the dual-decrement close-path
drift is fixed at the ROOT now, not deferred. #251 = **Part A**
(`max(proxy, broker_floor)` never-fail-open raise) **+ Part B**
(idempotent close-decrement — §2b). A focused expert pass designed B
and found a fatal objection (no shared chokepoint) addressed by the
funnel step below.

## 0. Locked constraints (do NOT re-litigate)

- **NEVER FAIL OPEN.** The gate may over-count → wrongly BLOCK a
  trade (lost opportunity, acceptable) but must NEVER under-count →
  wrongly ALLOW more concurrent positions than the per-engine limit
  (uncontrolled capital risk). Every change is measured against this.
- The current conservative proxy is the never-fail-open **baseline**;
  no path may ever produce `effective_open < proxy`.
- No engine has graduated (momentum/sentinel paper-trade today) — the
  *current* real-world blast radius is limited, but the gate is the
  live risk control and must be correct for graduation.

## 1. The real gap (precisely — corrects the TODO's premise)

The TODO says "stale prior-holding slots not reconciled… scheduler
restart". **The restart fear is wrong:** the live store is
`PostgresRiskStateStore` (`momentum/scheduler.py:252`,
`sentinel/scheduler.py:135`) persisting `open_positions` to
`platform.risk_state`; `register_engine` is idempotent and won't
clobber (`governor.py:224-227`). **The proxy survives restarts — no
latent restart fail-open.** This is correctly a *follow-up*, not an
emergency.

**The actual defect is an open/close count-source asymmetry:**
- **Open (synchronous, tight):** `gate_batch_order` does
  `record_fill(position_delta=+1)` on ALLOW *before* `broker.
  place_order` (`batch_gate.py:50-52`, momentum:411-426). A failed
  `place_order` leaves +1 with no position → over-count → safe.
- **Close (asynchronous, drift):** `-1` arrives via **two
  independent paths**: (a) the scheduler's own rebalance-sell
  `record_fill(-1)` (momentum:440-444) **and** (b) the
  `trade_monitor` stream on AAR close (`trade_monitor.py:616-621`).
  The same close can decrement **twice**; `max(0, …)`
  (`governor.py:397`) only floors at 0, never re-anchors to truth.
  Over rebalance cycles `open_positions` can **drift monotonically
  below true with no self-correction → eventual fail-open** against
  `max_open_positions` (`governor.py:319`).

**This dual-decrement under-count drift is the more material latent
risk** and the spec records it explicitly. The fix below *mitigates*
it (re-anchors to broker truth on every gate) without ever weakening
never-fail-open; the **root close-path dual-decrement fix is a
distinct DEFERRED item** (§5).

## 2. The frozen rule — reconciliation that can ONLY tighten

> **`effective_open = max(persisted_proxy, broker_floor)`**
>
> `broker_floor` = count from `RiskGovernor._broker.get_positions()`
> summed **across all engines** (no per-engine attribution today —
> see §5). Used **solely as a RAISE**. On ANY broker
> error / timeout / empty / exception → `broker_floor = 0` (a no-op
> against `max(...)`, the proxy stands). The proxy is **never**
> lowered by reconciliation; AAR / the trade-monitor stream are
> **never** consulted as a count oracle (they are the lagged write
> target — circular, and the lag is the fail-open source).

**Slot-in:** a new step in `RiskGovernor.check_trade` immediately
before the concurrent-position check (`governor.py:318→319`); the
check becomes `if max(state.open_positions, broker_floor) >=
limits.max_open_positions: BLOCK`. Gated behind a per-engine
`reconcile_open_floor: bool` in `tpcore/risk/limits_profile.py`
(**default False**; **True only for the batch engines** momentum +
sentinel). The broker handle already exists and `get_positions()` is
already called in-band at `governor.py:376` — no new wiring / no
async-context problem.

**Never-fail-open proof:** for all `broker_floor ≥ 0`,
`max(proxy, broker_floor) ≥ proxy`. The status-quo BLOCK threshold is
a strict lower bound on strictness; reconciliation can only make the
gate *stricter or equal*, never looser. No code path yields
`effective < proxy` (proxy is the conservative never-fail-open
baseline). ∎

## 3. Failure-mode table (must hold exactly)

| Broker source state | `broker_floor` | Decision vs today |
|---|---|---|
| up, returns N positions | N (cross-engine sum) | ≥ today (tighter or equal) |
| down / timeout / exception | 0 | identical to today (proxy only) |
| stale / under-reports | low | `max` → proxy stands; never below proxy |
| empty `[]` (incl. error-as-empty) | 0 | identical to today |

Cross-engine sum **over-counts** a single engine's slots (multiple
engines share one Alpaca account, no attribution) → strictly tighter
→ still never-fail-open. Documented limitation, acceptable.

## 2b. Part B — idempotent close-decrement (ROOT fix; operator-approved in-scope)

**Fatal objection (must be fixed FIRST, mandatory step 1):** the two
`-1` paths do NOT share a chokepoint — the scheduler rebalance-sell
loop (`momentum/scheduler.py:440`, `sentinel/scheduler.py:283`) calls
`RiskGovernor.record_fill()` (`governor.py:383`, read-modify-write +
`store.put()`), while the trade-monitor stream
(`trade_monitor.py:618`) calls `PostgresRiskStateStore.record_fill()`
directly; `put()` is a full-row last-writer-wins upsert
(`persistent_store.py:68-100`) with NO `FOR UPDATE` / NO atomic
decrement. So a dedupe key cannot be enforced until both paths funnel
through ONE primitive.

**The design (frozen):**
- **Step 1 — funnel:** introduce one idempotent
  `RiskStateStore.record_close(engine, trade_id, realized_pnl)`; route
  BOTH `-1` close callers through it. The scheduler sell loop must
  pass the originating `trade_id` of the position it is closing (the
  one piece of new plumbing — derive from the AAR-open/position row it
  is selling out of). `record_fill`'s non-close (`+1` open / pnl-only)
  behaviour is **unchanged**; only the `-1` close routes to
  `record_close`.
- **Dedupe key = `(engine, trade_id)`** — the only stable
  unique-per-real-close id present (or trivially derivable) on BOTH
  paths (stream has `row.trade_id` directly; scheduler passes the
  originating trade_id). No broker/Alpaca id is on both paths.
- **Ledger + atomic arbiter:** new
  `platform.risk_close_ledger(engine text, trade_id text,
  recorded_at timestamptz default now(), PRIMARY KEY(engine,
  trade_id))`. `record_close` does, in ONE transaction:
  `INSERT … ON CONFLICT DO NOTHING`; **iff the insert won
  (rowcount=1)** → `UPDATE risk_state SET open_positions =
  GREATEST(0, open_positions-1), daily_pnl/weekly_pnl += pnl`; else
  (already counted by the other path / race loser) → COMMIT, NO
  decrement. Bounded by a daily prune `DELETE … WHERE recorded_at <
  now() - interval '14 days'` (a settled trade_id is never re-closed
  — age-ring, not unbounded).
- **`trade_id` null/absent → SKIP the decrement + WARN** (over-count
  → tight → safe; never guess).

**Never-fail-open proof (close decrements AT MOST once):** the
unique-key INSERT is the sole arbiter; only the insert-winner
decrements. Every interleaving — A→B, B→A, concurrent (one wins,
other `DO NOTHING`/unique-violation → skip), one path missing, ledger
INSERT error/txn abort (rolled back → no decrement), null trade_id
(skip) — yields **≤1** decrement, and every uncertainty branch
**skips** (→ over-count → tight → SAFE), never double-applies. ∎

**Composition with Part A:** orthogonal/layered. B makes the proxy
*exact* (kills the monotonic under-drift); A (`max(proxy,
broker_floor)`) is the independent last-line fail-safe for any
*other* proxy wrongness (cold start, manual broker action, ledger
prune edge). Neither weakens nor makes the other redundant — both
ship.

## 4. Phasing (gated PR per phase; subagent-driven)

| Phase | Deliverable |
|---|---|
| **B1** | **Part B — funnel + idempotent `record_close` + ledger (the root fix; lands FIRST — it removes the under-drift the rest relies on).** New `platform.risk_close_ledger` Alembic migration (PK `(engine,trade_id)`). One idempotent `RiskStateStore.record_close(engine, trade_id, realized_pnl)` doing the single-txn `INSERT … ON CONFLICT DO NOTHING` → decrement-iff-insert-won → else no-op; null `trade_id` → skip+WARN. Route BOTH `-1` close callers through it: the trade-monitor stream (`trade_monitor.py:618`) and the scheduler rebalance-sell loops (`momentum/scheduler.py:440`, `sentinel/scheduler.py:283`) — the scheduler must pass the originating `trade_id` (derive from the AAR-open/position row it sells out of). `record_fill` non-close (`+1`/pnl-only) behaviour byte-unchanged. Daily 14-day ledger prune (wire into the existing data-ops/maintenance cadence — no new daemon). TDD: the never-fail-open interleaving table (A→B, B→A, concurrent, one-path-missing, ledger-error→no-decrement, null-trade_id→skip) each a test that genuinely bites; an idempotency property test (same `(engine,trade_id)` N times ⇒ exactly one decrement); existing governor suite green. One gated PR. |
| **A1** | **Part A — `max(proxy, broker_floor)` raise + invariant test.** `tpcore/risk/limits_profile.py`: add `reconcile_open_floor` (default False; True for momentum+sentinel). `RiskGovernor.check_trade`: compute `broker_floor` (reuse the existing in-band `_broker.get_positions()` result — do NOT add a second round-trip; hoist/share if the existing call is after the position check), broker-error/timeout/empty→0, `effective = max(state.open_positions, broker_floor)`, used in the concurrent-position check ONLY when the per-engine flag is set (else byte-identical to today). TDD: broker-higher → tighter BLOCK; broker-down/timeout/exception/empty → identical to proxy-only; a **property test: NO input yields `effective < proxy`**; flag-off engines byte-unchanged; `test_max_concurrent_positions_blocks` + governor suite green. One gated PR. |
| **D1** | **Docs reconciliation.** TODO §Governor-follow-ups item → resolved (A+B shipped, root fixed not deferred); risk/governor design note + CLAUDE.md risk line if it enumerates batch-gate behaviour; this spec → BUILT + build record; memory note. One gated PR. |

Sequencing rationale: **B1 before A1** — B removes the structural under-drift; A is then the independent last-line fail-safe layered on a now-exact proxy. (A is still never-fail-open on its own, so order is for clarity, not safety-dependency.)

## 5. DEFERRED / OUT

- **DEFERRED:** per-engine broker attribution (needs
  `client_order_id` engine tagging) — separate ticket; the
  cross-engine broker over-count is safe (strictly tighter) meanwhile.
- **OUT (forbidden):** any reconciliation/decrement path that can
  *lower* the effective count below the conservative proxy; trusting
  AAR / the trade-monitor stream as a count *oracle* (the stream is
  only a `record_close` *caller*, arbitrated by the ledger — never a
  source of truth read back); broker-error → anything but `0`; a
  second broker round-trip at gate time; ANY `-1` close path that
  bypasses the idempotent `record_close` arbiter; changing the `+1`
  open path or `record_fill`'s non-close behaviour.

## 6. Open questions — RESOLVED by the scoping pass

- Restart fail-open? **No** — `PostgresRiskStateStore` persists the
  proxy (corrects the TODO premise).
- Safe exact source? **None to lower with**; broker only as a raise,
  error→0.
- Broker handle at gate time? **Yes**, already in-band
  (`governor.py:376`) — reuse, no new wiring.
- Net: a never-fail-open `max(proxy, broker_floor)` raise, opt-in per
  batch engine; the more-material dual-decrement drift mitigated here
  + recorded as a distinct deferred fix.

**Spec ready for the operator review gate** — it changes a live-money
risk-control's counting (never-fail-open formally proven) and
consciously DEFERS the more-material dual-decrement root fix (a
scope/decision the operator should bless).
