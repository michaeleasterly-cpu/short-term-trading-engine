# Sub-project C — Allocator → Event-Driven — Design

**Status:** approved 2026-05-17 (operator; approach #1 + Q1/Q2/Q3).
Sub-project C of the event-driven engine epic
(`2026-05-17-event-driven-engine-services-design.md`, A→B→C→D).
A (`engine_profile`/`should_fire`) and B (`ops/engine_dispatch.py`)
are merged. C folds the allocator into the same event-driven model
and retires its launchd cron.

## 1. Problem

The allocator (`tpcore/allocator/service.py` `AllocatorService.run_once`,
invoked via `scripts/ops.py --allocate` → `cmd_allocate`) is the lone
time-driven outlier: a launchd `LaunchAgent`
(`scripts/install_launchd_allocator.sh`) runs it Monday-only at
13:00 UTC. It has no "already ran this week" guard (only the
`(engine, allocation_date)` upsert unique constraint), and it writes
`platform.risk_state.engine_equity` — which the RiskGovernor and every
engine read. It is `engine_profile`-profiled (`allocator:
WEEKLY_FIRST_TRADING_DAY`, added dark in A) but nothing consumes that.

## 2. Architecture

The allocator becomes the **first gated step inside
`ops/engine_dispatch.py`'s `dispatch_once`**, before the engine ROSTER
loop. One event path: `data-ops daemon → DATA_OPERATIONS_COMPLETE →
engine_service → run_all_engines.sh → ops.engine_dispatch` (B,
unchanged) now also runs the allocator first. Allocator-before-engines
ordering is guaranteed **by construction** (sequential in
`dispatch_once`). The Mon launchd cron is retired. Deployment-agnostic
& idempotent-on-restart (Railway-portable), inheriting B's properties.

## 3. Dispatch flow

`dispatch_once(pool, now)` — NEW first step, then the unchanged B
ROSTER loop:

1. `await _dispatch_allocator(pool, now)`:
   a. `decision = await should_fire("allocator", now, pool)` (WEEKLY
      cadence boundary + market-closed + data-ready + not-already-run
      — B's exact gate, no special-casing of the gate logic).
   b. `decision.fire` → run the allocator via a NEW allocator-specific
      invoke `_invoke_allocator()` (NOT B's `_invoke_scheduler`, which
      runs `python -m {engine}.scheduler`): subprocess the EXACT
      canonical command the launchd cron runs today —
      `python scripts/ops.py --allocate` (Q2; zero behavior drift, no
      one-off, crash-isolated, dispatcher owns no allocator capital
      config). `_invoke_allocator` is wrapped in the SAME try/except
      isolation idiom as B's `_safe_invoke` (a spawn error logs +
      proceeds, never aborts the sweep). Await it.
      - exit 0 → `logger.info("engine_dispatch.allocator_done")`.
      - non-zero / raised → **`logger.error(
        "engine_dispatch.allocator_failed", returncode=…)`** (operator
        alarm) and **return normally** so the engine ROSTER loop still
        runs on the persisted prior-week `risk_state.engine_equity`
        (Q3 — degraded-not-broken; never abort the daily sweep).
      - **Plan-time verification (no assumption):** confirm
        `scripts/ops.py --allocate`'s REAL exit-code contract — does
        `cmd_allocate`/`amain` exit non-zero when `AllocatorService.
        run_once` fails/escalates? If it does NOT reliably non-zero on
        allocator failure, the failure-detection must instead key off
        the allocator's persisted signal in `application_log` (the
        §4 `SHUTDOWN exit_code` or an `ALLOCATOR_*`/error event) within
        this cadence window — pin the exact mechanism in the plan from
        the real `ops.py`/`cmd_allocate` code. The Q3 behavior
        (alarm + proceed) is unchanged regardless of detection
        mechanism.
   c. `not decision.fire` and `checks["data_ready"] is False` → emit
      `ENGINE_DATA_REQUEST` for `engine="allocator"` via B's EXISTING
      `_emit_data_request`/`_open_request_state` path (the locked
      inter-lane contract applies UNIFORMLY — the data lane already
      heals any engine string; `sources` come from
      `failing_sources_for_engine("allocator")`). Then proceed to the
      ROSTER loop (engines run on prior equity; allocator retries next
      readiness cycle / on `DATA_REPAIR_COMPLETE`).
   d. `not decision.fire` for cadence/market/already-ran → log skip
      reason, proceed. (B's `DATA_REPAIR_COMPLETE` re-dispatch +
      crashed-STARTUP guard apply to "allocator" too via the SAME
      helpers — reused, not duplicated.)
2. The unchanged B engine ROSTER loop.

## 4. Prerequisite — allocator STARTUP/SHUTDOWN instrumentation

`should_fire("allocator")._already_ran` keys idempotency off a
`STARTUP` row in `platform.application_log` within the cadence window.
`AllocatorService.run_once` instantiates `DBLogHandler(engine=
"allocator")` and emits `ALLOCATOR_*` events but **never `STARTUP`/
`SHUTDOWN`** — the identical gap momentum had pre-T4b. Without this,
the allocator's WEEKLY idempotency is non-functional → it would re-run
on every readiness event in its first-trading-day-of-week window
(idempotent on `(engine, allocation_date)` so no data corruption, but
wasted heavy compute + the crashed-STARTUP guard inert). **Required
task:** add `await self._db_log.startup(...)` at run-start (before any
decision/IO logic) and `await self._db_log.shutdown(duration_ms,
exit_code)` in a `finally:` covering EVERY exit path (skip, success,
exception), mirroring `reversion`/momentum-T4b exactly. DBLogHandler is
already wired (`service.py:163`) — instrumentation only, ZERO
allocation/sizing/freeze logic change.

## 5. Per-engine data gate for the allocator

`should_fire` calls `capital_gate.assert_passed_for_engine(pool,
"allocator")`; `"allocator"` is not in `ENGINE_TABLES` → today that
falls back to the over-broad global all-green (`EXPECTED_SOURCES`).
Add `"allocator": frozenset({"prices_daily"})` to
`capital_gate.ENGINE_TABLES` — `prices_daily` (SPY) is the allocator's
only validation-gated input (regime/CHOP); AAR/risk_state are engine
*output* tables, not validation-gated. This gates the allocator on its
REAL data dependency per the established per-engine-gate model (not the
global fail-safe), and makes `failing_sources_for_engine("allocator")`
return the right `HealSpec.source` for the `ENGINE_DATA_REQUEST` path.
Reuses the existing registry-coverage-test mechanism; no new gate code.

## 6. Launchd retirement

Remove `install_launchd_allocator` from the loop in
`scripts/install_all_daemons.sh` (the 5-daemon installer) and retire
`scripts/install_launchd_allocator.sh` (delete it; the event path
fully replaces it). Update the installer's summary/log line + any docs
(CLAUDE.md `tpcore/allocator/` line, `docs/OPERATIONS.md`) that say
"daemon Mon 13:00 UTC" → "event-driven via engine_dispatch
(WEEKLY_FIRST_TRADING_DAY)". The launchd-installed plist on the
operator's machine is removed by re-running `install_all_daemons.sh`
(documented); not C's job to uninstall a live plist.

## 7. Idempotency / ordering / Railway

- Idempotency: `should_fire("allocator")` STARTUP-window check (§4) +
  the allocator's existing `(engine, allocation_date)` upsert
  constraint = belt-and-suspenders. A re-fired readiness event in the
  same week → `should_fire` returns "already ran this cycle" → skip.
- Ordering: allocator runs to completion before the ROSTER loop
  (sequential `await`) — engines/governor read the freshly-written
  `engine_equity`. On allocator failure they read the valid prior
  value (Q3).
- Railway: no launchd/macOS coupling; the canonical
  `python scripts/ops.py --allocate` subprocess + env/pool conventions
  port as-is; restart-safe via STARTUP idempotency.

## 8. Testing

Unit tests (fake pool, mocked `should_fire`/subprocess/`failing_
sources_for_engine`, no real DB):
- fire → allocator subprocess invoked exactly once, BEFORE any engine
  scheduler invoke (ordering assertion).
- allocator subprocess non-zero/raises → `allocator_failed` alarm
  logged AND the engine ROSTER loop still runs (Q3).
- data_ready False for "allocator" → one `ENGINE_DATA_REQUEST`
  (`engine="allocator"`, `sources` from `failing_sources_for_engine`),
  allocator NOT run, engines still proceed.
- off-cadence/market-open/already-ran "allocator" → skip, no invoke.
- allocator STARTUP/SHUTDOWN: a `run_once` call emits STARTUP (before
  decision logic) and SHUTDOWN in `finally` on success AND on the
  skip/early-return AND exception paths; exit_code 0/non-zero.
- `failing_sources_for_engine("allocator")` returns `["prices_daily"]`
  when that check is red; `[]` all-green.
- `capital_gate.ENGINE_TABLES["allocator"] == frozenset({"prices_daily"})`;
  registry-coverage test still green.
- `install_all_daemons.sh` no longer references `install_launchd_allocator`;
  `bash -n` clean.

## 9. Scope boundary

C delivers: the `_dispatch_allocator` first step + wiring in
`ops/engine_dispatch.py`; allocator STARTUP/SHUTDOWN instrumentation in
`tpcore/allocator/service.py`; `capital_gate.ENGINE_TABLES` allocator
entry (+ test); launchd retirement (`install_all_daemons.sh`, delete
`install_launchd_allocator.sh`, doc lines); tests. C does **NOT** touch
`tpcore/selfheal`, `tpcore/feeds`, `tpcore/ingestion`, `reversion/`,
`vector/`, `ops/data_repair_service.py` (data lane), nor the
allocator's allocation/sizing/freeze strategy logic (instrumentation
only). Acceptance: allocator is profile-gated + ordered-before-engines
in the one event path; a failed/blocked allocator never aborts the
engine sweep; launchd cron retired; full suite green; ruff/check_imports
clean.

## 10. Decisions log

- **D-C1 (Q1)** Allocator = first gated step inside `dispatch_once`
  (before the ROSTER loop); ordering guaranteed by construction; one
  event path; launchd cron retired.
- **D-C2 (Q2)** Invoke via subprocess `python scripts/ops.py
  --allocate` (the exact canonical launchd command; no one-off; thin
  dispatcher; crash-isolated).
- **D-C3 (Q3)** Allocator failure → `logger.error` operator alarm +
  engine sweep proceeds on prior persisted `engine_equity`; NEVER
  abort the sweep (weekly rebalance is a refinement, not a hard daily
  precondition).
- **D-C4 (prereq)** Instrument allocator `STARTUP`/`SHUTDOWN`
  (DBLogHandler already wired) so `should_fire` idempotency works —
  T4b-pattern, instrumentation only.
- **D-C5** Add `ENGINE_TABLES["allocator"] = {"prices_daily"}` so the
  allocator gates on its real validation-gated input per the
  per-engine-gate model (not the global fail-safe).
