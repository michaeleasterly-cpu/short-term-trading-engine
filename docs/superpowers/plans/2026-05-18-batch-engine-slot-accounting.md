# Batch-Engine Slot Accounting (#251) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]` checkboxes.

**Goal:** Make the batch-engine (momentum/sentinel) concurrent-position count correct and provably never-fail-open: (B) an idempotent close-decrement that kills the dual-decrement under-drift at the root, then (A) a `max(proxy, broker_floor)` last-line raise.

**Architecture:** Spec `docs/superpowers/specs/2026-05-18-batch-engine-slot-accounting-design.md` (**v1.2** — post-B1 review correction). Live-money RiskGovernor / `tpcore.risk`. Sacred invariant: **never fail open**. **v1.2 correction:** B1's funneled pair (scheduler-sell vs stream) is disjoint by engine — B1 is a correct never-fail-open **hardening + the reusable `record_close`/ledger primitive, NOT the root fix**. The REAL dual-decrement = reversion/vector `order_manager.reconcile()` `-1` vs the trade-monitor stream `-1` (same per-trade engines) — fixed in **Phase B2** (spec §2c). Phases: **B1** (hardening+primitive — SHIPPED) → **B2** (the real fix; starts with a make-or-break key-identity expert gate) → **A1** (`max(proxy, broker_floor)`) → **D1** (docs). B1 task text below is historical (it built the primitive); read §2c + the B2 section for the real fix.

**Tech Stack:** Python 3.11, asyncpg, Alembic (`platform/migrations`), pydantic v2, structlog, pytest (`asyncio_mode=auto`), ruff. Gated PR per phase; CI-green before merge; branch-hygiene (`git switch -c`, verify branch before every commit; tests never touch real repo/`data/`/real DB — fake pool/store).

**Reference (read, do not re-derive):** `tpcore/risk/governor.py` (`RiskGovernor.record_fill`, `check_trade`, the `:318→319` concurrent-position check, the in-band `_broker.get_positions()` at ~`:376`), `tpcore/risk/persistent_store.py` (`PostgresRiskStateStore.record_fill`/`put` ~`:68-100`), `tpcore/risk/limits_profile.py` (`limits_for`), `tpcore/risk/batch_gate.py` (`gate_batch_order`), `tpcore/trade_monitor.py` ~`:596-621` (stream close `-1` + `row.trade_id`), `momentum/scheduler.py` ~`:252,440` + `sentinel/scheduler.py` ~`:135,283` (the rebalance-sell `-1` + the store wiring), `platform/migrations/` (latest revision + the existing `risk_state` table migration for style), the existing risk test suite (`grep -rl "RiskGovernor\|record_fill\|risk_state\|test_max_concurrent" tpcore/tests tests`).

---

## Phase B1 — idempotent close-decrement (ROOT fix; gated PR #1; lands first)

Branch `feat/risk-idempotent-close-b1` off fresh `main`.

**Files:**
- Create: `platform/migrations/versions/<rev>_risk_close_ledger.py`
- Modify: `tpcore/risk/persistent_store.py` (+ the in-memory store + the `RiskStateStore` protocol/ABC — wherever the store interface is defined) — add `record_close`
- Modify: `tpcore/risk/governor.py` (route the close `-1` through `record_close`; `record_fill` non-close unchanged)
- Modify: `tpcore/trade_monitor.py` (stream close → `record_close(engine, trade_id, pnl)`)
- Modify: `momentum/scheduler.py`, `sentinel/scheduler.py` (rebalance-sell → `record_close`, passing originating `trade_id`)
- Modify: the data-ops/maintenance cadence caller for the 14-day ledger prune (no new daemon — reuse the existing maintenance stage; identify it by reading how other periodic prunes are wired)
- Test: `tpcore/tests/test_risk_close_ledger.py` (+ extend existing risk tests)

### Task B1.1: the ledger migration (TDD-light: migration + a schema test)
- [ ] **Step 1:** Read the latest `platform/migrations/versions/*` head + the original `risk_state` table migration for house style (naming, `down_revision`, `op.create_table`).
- [ ] **Step 2:** Create the migration: `platform.risk_close_ledger(engine text NOT NULL, trade_id text NOT NULL, recorded_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (engine, trade_id))`. Downgrade drops it. Chain `down_revision` to the current head.
- [ ] **Step 3:** A test asserting the migration head is linear (no multiple heads) and the table/PK is as specified (mirror any existing migration test; if none, a minimal `alembic` upgrade-on-a-sqlite/tmp or a structural assertion per the repo's migration-test convention — read how other migrations are tested).
- [ ] **Step 4:** Run migration tests → pass. Commit.

### Task B1.2: idempotent `record_close` primitive (TDD)
- [ ] **Step 1:** Read the `RiskStateStore` interface + `PostgresRiskStateStore.record_fill`/`put` and the in-memory store. Identify the exact transaction/connection idiom used (asyncpg `async with conn.transaction()`).
- [ ] **Step 2 (failing tests, fake pool/store):** define `record_close(engine: str, trade_id: str | None, realized_pnl: Decimal) -> bool` (returns True iff it applied the decrement). Required behaviour: ONE transaction — `INSERT INTO platform.risk_close_ledger(engine,trade_id) VALUES($1,$2) ON CONFLICT DO NOTHING`; if inserted (rowcount==1) → `UPDATE platform.risk_state SET open_positions = GREATEST(0, open_positions-1), daily_pnl = daily_pnl + $3, weekly_pnl = weekly_pnl + $3, updated_at = now() WHERE engine=$1` and return True; else (conflict / already counted) → return False, NO decrement; `trade_id is None` → log structlog WARN, return False, NO decrement (over-count = safe). Implement the same contract for the in-memory store (a set of seen `(engine,trade_id)`; same skip semantics). Tests: first call decrements once + returns True; duplicate `(engine,trade_id)` → no decrement + False (idempotent); distinct trade_ids each decrement; `trade_id=None` → no decrement + WARN; pnl is applied exactly once (with the single decrement) and NOT on the deduped call; `GREATEST(0,…)` floor preserved. Run → FAIL.
- [ ] **Step 3:** Implement on both stores. Do NOT change `record_fill`'s non-close path.
- [ ] **Step 4:** Run → PASS. Commit.

### Task B1.3: funnel BOTH `-1` close callers through `record_close` (TDD)
- [ ] **Step 1:** Read the three close-decrement sites: `tpcore/trade_monitor.py` ~:618 (has `row.trade_id`, `row.engine`, realized pnl), `momentum/scheduler.py` ~:440 + `sentinel/scheduler.py` ~:283 (the rebalance-sell loop — find where the originating `trade_id` of the position being sold is available: the AAR-open / position row it iterates; if not currently in scope, add the minimal lookup to obtain it).
- [ ] **Step 2 (failing tests):** assert the trade-monitor stream close path now calls `record_close(engine, trade_id, pnl)` (not the old raw `record_fill(position_delta=-1)`); assert the momentum + sentinel rebalance-sell paths call `record_close` with the originating trade_id; **the never-fail-open interleaving suite**: simulate stream-then-scheduler, scheduler-then-stream, concurrent (both attempt the same `(engine,trade_id)`), one-path-only, ledger-INSERT-error (→ no decrement, exception contained, gate stays tight), null-trade_id (→ skip+WARN) — in EVERY case `open_positions` decrements **exactly once or zero, never twice**; an idempotency property test (same `(engine,trade_id)` applied N times via both paths ⇒ exactly one net `-1`). Run → FAIL.
- [ ] **Step 3:** Reroute all three callers to `record_close`. The `+1` open path and `record_fill` non-close behaviour byte-unchanged. Stream/scheduler close-failure must stay crash-isolated (a `record_close` error must not kill the stream or the scheduler loop — mirror their existing error-isolation).
- [ ] **Step 4:** Run → PASS (interleaving + idempotency suite green). Commit.

### Task B1.4: bounded 14-day ledger prune (TDD)
- [ ] **Step 1:** Find how existing periodic prunes/maintenance are wired (no new daemon — e.g. an `ops.py` maintenance stage / the data-ops cadence). Read one for the pattern.
- [ ] **Step 2 (failing test):** a prune fn `DELETE FROM platform.risk_close_ledger WHERE recorded_at < now() - interval '14 days'` wired into the existing cadence; test it deletes only >14d rows, keeps recent, is idempotent, and (safety) pruning a still-relevant row cannot cause a re-decrement (a settled trade_id is never re-closed — assert the close path won't re-fire for a pruned id under normal flow). Run → FAIL.
- [ ] **Step 3:** Implement + wire into the existing maintenance caller.
- [ ] **Step 4:** Run → PASS. Commit.

### Task B1.5: Phase-B1 verify + PR
- [ ] **Step 1:** FULL suite `python -m pytest tpcore/tests/ tests/ scripts/tests/ -q 2>&1 | tail -3` → 0 failed, ≥ baseline + new; existing governor/risk suite (esp. `test_max_concurrent_positions_blocks`, any `record_fill` tests) green; `ruff check` the changed files clean (no new noqa beyond documented precedent); `git -C <repo> branch --list 'llm-triage/*'` empty; real `data/`/no real DB touched (fake pool/store only); `git diff --name-only` = only the B1 files.
- [ ] **Step 2:** Push, gated PR, wait for the CI run to register then `gh run watch --exit-status`, squash-merge `--delete-branch`, sync `main`.

---

## Phase B2 — the REAL dual-decrement fix (reversion/vector order_manager vs stream) (gated PR #2)

Branch `feat/risk-idempotent-close-b2` off fresh `main` (post-B1). Spec §2c authoritative.

**Files:** Modify `reversion/order_manager.py` (~:241), `vector/order_manager.py` (~:241), and (only if the key-identity gate requires it) the OCO-submit site that writes `platform.open_orders.trade_id` and/or `tpcore/trade_monitor.py`. Test: `tpcore/tests/test_risk_close_b2_*.py`.

- [ ] **Task B2.0 — MAKE-OR-BREAK key-identity expert gate (read-only first).** Dispatch a focused expert pass: trace, in real code, exactly what string is written to `platform.open_orders.trade_id` at OCO/tier-1 submit (the value the trade-monitor stream later passes to `record_close` as `row.trade_id`) vs the `trade_key`/`cid` the `order_manager` holds at `reconcile()` time (currently `f"reversion-{trade_key}"` / `f"vector-{cid}"`). Establish ONE shared canonical `trade_id`. If they already match → B2.1 just reroutes. If they differ → **that mismatch is the core bug**; the fix is to make BOTH sides emit the one shared id (NOT a derived composite — that was B1's batch-path mistake). Output the frozen shared-id definition + which file(s) must change. Controller reviews before B2.1.
- [ ] **Task B2.1 — reroute + prove identity (TDD).** Route the `reversion/order_manager.py` + `vector/order_manager.py` `reconcile()` close `-1` through `record_close(engine, <shared trade_id>, realized_pnl)` (the B1 arbiter). Preserve each caller's existing crash-isolation; `+1` open path untouched. TDD: (a) a key-identity test asserting the order_manager's `record_close` trade_id == the `open_orders.trade_id` the stream uses for the SAME OCO pair — and it must genuinely bite if they diverge; (b) the real interleaving — order_manager-reconcile-then-stream, stream-then-reconcile, concurrent — for the same shared key decrements **exactly once**, fails (pre-B2) at twice; (c) existing reversion/vector order-manager + governor suites green; (d) never-fail-open inherited from `record_close` (uncertainty→skip→over-count). One gated PR.
- [ ] **Task B2.2 — verify + PR.** FULL suite 0 failed; reversion/vector + governor suites green; ruff/no-leak/no-real-IO; `git diff --name-only` = the B2 files only; gated PR, CI-green (registered→watch), squash-merge, sync.

## Phase A1 — `max(proxy, broker_floor)` never-fail-open raise (gated PR #3)

Branch `feat/risk-broker-floor-a1` off fresh `main` (post-B1).

**Files:** Modify `tpcore/risk/limits_profile.py` (add `reconcile_open_floor`), `tpcore/risk/governor.py` (`check_trade`); Test: extend the governor test suite.

### Task A1.1: `reconcile_open_floor` flag (TDD)
- [ ] **Step 1:** Read `tpcore/risk/limits_profile.py` `limits_for`/the `RiskLimits` shape.
- [ ] **Step 2 (failing test):** add `reconcile_open_floor: bool` (default **False**; **True** for `momentum` and `sentinel` only). Test: momentum/sentinel → True; every other engine → False; default False.
- [ ] **Step 3:** Implement (additive field; do not change existing limit values).
- [ ] **Step 4:** Run → PASS. Commit.

### Task A1.2: the `max(proxy, broker_floor)` raise (TDD)
- [ ] **Step 1:** Read `RiskGovernor.check_trade` — the concurrent-position check (~`:318→319`) and the existing in-band `_broker.get_positions()` call (~`:376`). Decide: reuse/hoist that result (do NOT add a second broker round-trip). Determine its cross-engine position-count semantics.
- [ ] **Step 2 (failing tests):** when `limits.reconcile_open_floor` is True: `broker_floor` = count from the existing `_broker.get_positions()` result (cross-engine sum); on broker error/timeout/exception/empty → `broker_floor = 0`; the concurrent-position check uses `effective = max(state.open_positions, broker_floor)`. Tests: broker returns higher → tighter BLOCK; broker down/timeout/exception/empty → identical to proxy-only (never looser); **property/invariant test: for all (proxy, broker_floor≥0), `effective >= proxy` — NO input yields `effective < proxy`**; flag-off engine → byte-identical to today (the check still uses raw `open_positions`); existing `test_max_concurrent_positions_blocks` + governor suite green. Run → FAIL.
- [ ] **Step 3:** Implement; flag-gated; reuse the existing broker call result (hoist if needed); broker-error→0; no second round-trip; non-flagged path byte-identical.
- [ ] **Step 4:** Run → PASS. Commit.

### Task A1.3: Phase-A1 verify + PR
- [ ] **Step 1:** FULL suite 0 failed; governor suite green; ruff clean; `git diff --name-only` = the 2 files + tests; no real broker/DB call in tests (faked). 
- [ ] **Step 2:** Push, gated PR, CI-green (registered→watch), squash-merge, sync.

---

## Phase D1 — docs reconciliation (gated PR #3)

Branch `docs/risk-slot-accounting-d1` off fresh `main`.

**Files:** `TODO.md` (§Governor follow-ups item → resolved: A+B shipped, root fixed not deferred; the per-engine-attribution item recorded as the remaining deferred), the risk/governor design doc + `CLAUDE.md` risk line if it enumerates batch-gate/`open_positions` behaviour, this spec → `Status: BUILT 2026-05-18` + Build record (B1 #, A1 #, D1 #), a memory note.

- [ ] **Step 1:** Reconcile each doc to shipped reality (accuracy discipline — cross-check claims vs merged B1/A1 code; no overclaim; do NOT claim per-engine attribution which is still deferred).
- [ ] **Step 2:** `git diff --stat` = docs only; collection clean; gated PR; CI-green; squash-merge; sync. Update the memory note (#251 → BUILT; the per-engine-attribution follow-up recorded).

---

## Self-Review

**1. Spec coverage:** §0 never-fail-open → enforced in every B1/A1 test (skip/raise-toward-strict on all uncertainty; the `effective>=proxy` + ≤1-decrement property tests); §2 Part A rule → A1; §2b Part B funnel+ledger+arbiter+key+null-skip → B1.1–B1.4; §3 failure-mode table → A1.2 tests; §4 phasing B1→A1→D1 → Phases; §5 deferred per-engine-attribution + OUT (no lowering path, no bypass of `record_close`, no `+1`/`record_fill`-nonclose change, broker-error→0, no 2nd round-trip) → enforced as explicit test assertions/constraints in B1.3 + A1.2. ✓

**2. Placeholder scan:** the only "find it by reading" items (the scheduler's originating-trade_id source; the existing periodic-prune cadence caller; the migration-test convention) are explicit read-then-wire steps grounded in named files, not TBDs. Every task has files + failing test + impl + verify + commit.

**3. Type/name consistency:** `record_close(engine:str, trade_id:str|None, realized_pnl:Decimal)->bool` consistent across both stores + all 3 callers + tests; `risk_close_ledger(engine,trade_id,recorded_at)` PK `(engine,trade_id)` consistent migration↔primitive↔prune; `reconcile_open_floor` flag name consistent limits_profile↔governor↔tests; `effective = max(state.open_positions, broker_floor)` consistent §2/§3/A1.2.

Execution: subagent-driven-development — fresh implementer per phase, split spec-then-code-quality reviews (adversarial on never-fail-open), gated PR per phase, CI-green before merge, branch-hygiene before every commit. B1 before A1.
