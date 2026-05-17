# DA-1 — Engine Supervisor / Escalation — Design

**Status:** approved 2026-05-17 (operator; mandate/enforcement/clear/
scope/approach all answered, design approved). Sub-project DA-1 of the
**Deterministic Agents epic** (DA-1 Supervisor → DA-2 AAR Auto-Tune →
DA-3 Two-Daemon Consolidation). Builds on the merged event-driven
engine epic (A `engine_profile`, B `engine_dispatch`, C allocator).

## 1. Problem

The data lane is fully autonomous: `ops/data_repair_service.py` runs a
bounded **detect → self-heal → verify → escalate** loop and emits
`DATA_REPAIR_ESCALATED` when bounded repair is exhausted. The engine
lane has no equivalent: `engine_dispatch` only *logs* an alarm on
engine-side infra failures (`engine_dispatch.data_request_timeout`,
the ad-hoc `_crashed_startup_refire`), nothing escalates them through
the inter-lane contract, and a structurally-broken or silently-dead
engine is never stood down. The engine lane lacks the data lane's
autonomy symmetry.

## 2. Architecture

A new `ops/engine_supervisor.py` — a pure, **bounded, deterministic
detect → self-heal → verify → escalate+hold → auto-clear** mechanism
for engine-lane **infra/liveness** failures. Invoked per dispatch
actor (every ROSTER engine + the allocator) by `ops/engine_dispatch.py`
inside the existing engine daemon — **no new daemon** (advances toward
DA-3's two-daemon target). Crash-isolated: any `supervise()` exception
is caught and the sweep proceeds — a broken supervisor must NEVER abort
the dispatch or block trading (same invariant as allocator-failure in
C, D-C3). It reuses the locked `platform.application_log` event bus and
the `tpcore.engine_profile.should_fire` gate SoT; it is the reusable
hold/escalate **primitive** DA-2 (behavioral) will consume.

## 3. State — event-sourced, no new table (D-DA1-5)

Hold state is **derived from `application_log` events**
(`ENGINE_HELD` / `ENGINE_CLEARED`) exactly as
`engine_dispatch._open_request_state` derives request state today
(latest `ENGINE_HELD` for an engine with no later `ENGINE_CLEARED` ⇒
held). Restart-safe, Railway-portable, zero schema/migration coupling
with the data session. A hold is keyed by `engine` + `hold_id` (uuid4,
supervisor-generated, the sole correlation key) + `failure_class`.

## 4. Detection surface — infra/liveness only (D-DA1-4)

Evaluated per actor each dispatch (DA-2 owns all behavioral/forensics
triggers; DA-1 explicitly excludes them):

- **`scheduler_crash`** — the scheduler subprocess exited non-zero
  (the dispatcher observes the return code directly when it invokes
  the actor; promote it into a supervisor signal, not just a log).
- **`crashed_startup`** — a `STARTUP` row in the cadence window with no
  clean completion (`SCAN_COMPLETE`/`SHUTDOWN`) older than the stale
  threshold. **Subsumes** `engine_dispatch._crashed_startup_refire`.
- **`data_request_timeout`** — an open `ENGINE_DATA_REQUEST` for the
  engine with no terminal within `_NO_TERMINAL_TIMEOUT_SECONDS`.
  **Subsumes** the current log-only detection in `_dispatch_engine`.
- **`data_repair_escalated`** — a `DATA_REPAIR_ESCALATED` terminal was
  emitted for this engine's request (the data lane exhausted bounded
  repair; the engine cannot run on absent data).
- **`missed_cycle`** — the engine had no `STARTUP` for N consecutive
  cadence windows in which it was `should_fire`-eligible (silent
  death; N a module constant, default 2). **A window in which the
  engine is already supervisor-held does NOT count as missed** — a
  held engine is intentionally not running; counting it would create
  a held → no-STARTUP → `missed_cycle` → re-invoke feedback loop.
  `missed_cycle` is evaluated only over non-held eligible windows.

## 5. Mechanism (per detected failure)

detect → **bounded, idempotent, class-specific self-heal**:
- `scheduler_crash` / `crashed_startup` / `missed_cycle` → re-invoke
  the actor's scheduler, bounded to ≤ `_MAX_REINVOKE` attempts
  (default 2), the attempt count read from event history (the
  generalised, bounded form of today's `_crashed_startup_refire`).
- `data_request_timeout` → re-emit exactly one fresh
  `ENGINE_DATA_REQUEST` (bounded re-ask; the data lane's
  one-terminal contract handles dedup/repair).
- `data_repair_escalated` → no self-heal possible (data lane already
  exhausted bounded repair) → straight to escalate+hold.

→ **verify** (re-evaluate the same condition after the bounded action).
→ resolved → emit `ENGINE_SUPERVISOR_RECOVERED` (INFO); **no hold**.
→ still unresolved after the bound → emit `ENGINE_ESCALATED`
  (operator alarm, parity with `DATA_REPAIR_ESCALATED`) **and**
  `ENGINE_HELD` (establishes the hold).

## 6. should_fire hold-gate (D-DA1-2)

`should_fire` gains a new **pure** precondition check. While an
`ENGINE_HELD` exists for the engine with no later `ENGINE_CLEARED`,
`should_fire` returns `FireDecision(False, "supervisor hold",
checks={... "supervisor_held": False ...})`. `should_fire` only
**reads** the event-sourced hold (a held engine simply never
dispatches); the supervisor is the sole **writer** of the hold events
— this preserves `should_fire` as a side-effect-free gate. Check
ordering: after `profiled`/`cadence`/`market_closed`, before
`data_ready` (a held engine should not even emit data requests).

## 7. Auto-clear — safe-by-construction (D-DA1-3)

The supervisor, each dispatch, for every held engine, evaluates a
**strong** per-class clear predicate (deliberately NOT "ran once"):

- infra classes (`scheduler_crash`, `crashed_startup`, `missed_cycle`,
  `data_request_timeout`): clear iff the engine produced a `STARTUP`
  **followed by** a clean `SHUTDOWN` with `exit_code == 0` in a
  cadence window strictly **after** the hold's `recorded_at`, AND no
  new failure was detected this cycle (a full clean verified cycle).
- `data_repair_escalated`: the above **and** a subsequent
  `DATA_REPAIR_COMPLETE green=true` for the engine's sources.

On pass → emit `ENGINE_CLEARED` (the §6 gate read then sees it; the
engine resumes next cycle). DA-1's predicate is conservative; DA-2's
behavioral clear will reuse this same `ENGINE_HELD`/`ENGINE_CLEARED`
primitive with an additional cooldown + condition-gone predicate.

## 8. Inter-lane contract additions (locked, schema:1)

Conventions identical to the existing contract: `hold_id` = uuid4,
supervisor-generated, sole correlation key; **no client timestamps**
(DB `recorded_at` only); one-terminal liveness — an `ENGINE_HELD` is
eventually followed by exactly one `ENGINE_CLEARED`.

- `ENGINE_ESCALATED` `{schema:1, hold_id, engine, failure_class,
  reason, attempts}` — operator alarm; weekly-digest MAY read it later
  (parity with `DATA_REPAIR_ESCALATED`; not DA-1 scope).
- `ENGINE_HELD` `{schema:1, hold_id, engine, failure_class, reason}`.
- `ENGINE_CLEARED` `{schema:1, hold_id, engine, clear_reason}`.
- `ENGINE_SUPERVISOR_RECOVERED` `{schema:1, engine, failure_class,
  attempts}` — INFO; bounded self-heal succeeded, no hold.

## 9. Composition with engine_dispatch — Approach A (D-DA1-5)

`dispatch_once` → for each actor (allocator first, then ROSTER):
`await engine_supervisor.supervise(pool, engine, now)` (idempotent,
crash-isolated) **then** the existing `_dispatch_engine(...)` whose
`should_fire` now includes the §6 hold check. `_crashed_startup_refire`
**migrates into** the supervisor as the `crashed_startup` self-heal
(single owner; `engine_dispatch` delegates) — a behavior-preserving
move, the B/C suites are the equivalence oracle (exactly as C-T1's
`_dispatch_engine` extraction). The current `data_request_timeout`
log-only branch becomes a supervisor escalation. The allocator is
supervised like an engine (a crashed `ops.py --allocate` is the
`scheduler_crash` class) — degraded-not-broken still holds (C/D-C3).

`supervise(pool, engine, now)` **persists its hold/clear events before
`_dispatch_engine` runs for that actor** (sequential `await` in the
per-actor step), so the same-cycle `should_fire` read in §6 observes a
hold/clear the supervisor just emitted this cycle — stand-down and
resume take effect immediately, with no cross-cycle lag.

## 10. Error handling

`supervise()` is fully crash-isolated (catch → `engine_supervisor.
error` structured log → return; the sweep proceeds). Everything is
bounded (attempt caps derived from event-history counts; no unbounded
loops). Idempotent: re-running `supervise()` within the same cadence
cycle produces no duplicate events — dedup on `(engine, failure_class,
cycle window)` for HELD/RECOVERED and on `hold_id` for CLEARED, the
same dedup discipline `engine_dispatch` uses for `ENGINE_DATA_REQUEST`.

## 11. Testing

Unit (fake pool, mocked `should_fire`/subprocess/event rows; no real
DB): each failure class → bounded self-heal attempted exactly N →
recovered (`ENGINE_SUPERVISOR_RECOVERED`, no hold) vs escalate
(`ENGINE_ESCALATED`+`ENGINE_HELD`) path. `should_fire` returns no-fire
`supervisor_held=False` when held; fires after `ENGINE_CLEARED`.
Auto-clear: a single STARTUP is **insufficient**; STARTUP + clean
`SHUTDOWN exit_code 0` in a post-hold window **is** sufficient;
`data_repair_escalated` additionally needs `DATA_REPAIR_COMPLETE
green=true`. Crash-isolation: a raising `supervise()` does not abort
`dispatch_once` (ROSTER still processed). Idempotency: double
`supervise()` in one cycle → no duplicate `ENGINE_HELD`. Migration:
`_crashed_startup_refire` behavior preserved (B/C dispatch suites
green, the oracle). Allocator supervised as an actor.

## 12. Scope boundary

DA-1 delivers: `ops/engine_supervisor.py` (the mechanism + the §4
infra detectors), the §6 `should_fire` hold-gate check, the §8 four
events, the §9 `engine_dispatch` wiring + behavior-preserving
migration of the two existing ad-hoc detections, and tests. DA-1 does
**NOT**: consume forensics/behavioral triggers (DA-2), consolidate
daemons (DA-3), touch data-lane files (`tpcore/selfheal`,
`tpcore/feeds`, `tpcore/ingestion`, `ops/data_repair_service.py`,
`ops/cutover_agent.py`, `ops/weekly_digest.py`), or change
allocation/risk/sizing logic. Acceptance: every §4 class detected;
bounded self-heal then escalate+hold; held engines gated off via
`should_fire`; conservative auto-clear; allocator covered; full suite
green; ruff/check_imports clean; B/C behavior preserved.

## 13. Decisions log

- **D-DA1-1** Mandate = **self-heal + autonomous stand-down** (detect →
  bounded remediation → if unresolved escalate AND gate the engine
  off), not pure escalation.
- **D-DA1-2** Stand-down enforcement = **`should_fire` hold-gate**
  (event-sourced `supervisor_held` check), not the `risk_state` kill
  switch — clean, reversible, dispatch-layer, no process-killing.
- **D-DA1-3** Clear = **auto-clear on clean cycle**, with a
  **safe-by-construction strong predicate** (§7: clean
  `SHUTDOWN exit_code 0` in a post-hold window, not "ran once"); no
  operator ack required.
- **D-DA1-4** DA-1 owns **infra/liveness** classes only and builds the
  reusable hold/escalate **mechanism**; DA-2 reuses it for
  **behavioral** (forensics) triggers. Clean DA-1/DA-2 boundary.
- **D-DA1-5** **Approach A** — supervisor module inside the engine
  daemon's dispatch path; event-sourced state (no new table);
  `_crashed_startup_refire` migrates in (behavior-preserving). Not a
  new poller daemon (would fight DA-3).
