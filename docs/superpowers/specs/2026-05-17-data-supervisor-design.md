# Data Supervisor — Per-Source Hold + Autonomous Auto-Clear — Design

**Status:** BUILT 2026-05-17 (DATA lane). Brainstorm → **spec (this
doc)** → plan → phased subagent build. Task #205. The data-native,
**symmetric (NOT copied)** counterpart of the engine lane's DA-1
`ops/engine_supervisor.py` + `tpcore/supervisor_state.py`.

**Build record:**
- P1 (PR #38): `tpcore/datasupervisor/` state vocabulary —
  `DATA_SOURCE_HELD`/`DATA_SOURCE_CLEARED`/`DATA_SOURCE_ESCALATED`,
  `DATA_SUPERVISOR_RECOVERED`, schema:1, event-sourced,
  `current_source_hold` view. Landed dark.
- P2 (PR #39): `datasupervise(pool, run_id)` — one bounded per-source
  pass; reuses selfheal/auditheal red predicates; opens per-source
  holds idempotently; autonomous auto-clear for recovered sources;
  bounded-escalates a source stuck ≥3 held cycles; crash-isolated.
  Thin `python -m tpcore.datasupervisor` entry point. Landed dark.
- P3 (PR #40): wire Step 4d — `run_data_operations.sh` Step 4d calls
  `python -m tpcore.datasupervisor` after self-heal (Step 4) and
  deep-audit (Step 4c); state-tracking only, never gates, `DATA_OPERATIONS_COMPLETE` invariant preserved.
- P4 (this doc update): CLAUDE.md + spec reconciled to shipped reality.

**Symmetry, not copy** (operator directive, saved memory): this reuses
DA-1's *pattern + locked contract shape + decision framework* (bounded
detect → escalate+hold → safe auto-clear; event-sourced hold, no new
table; Approach-A no-new-daemon; schema:1, `hold_id` uuid4 sole
correlation key, no client timestamps, one-terminal liveness) but is
designed **data-native**: per **source/feed** (not per engine);
selfheal/auditheal/contract-sentinel are the bounded-repair agents
(the Supervisor does NOT re-heal); stand-down is the *existing* sacred
whole-cycle emit gate + per-engine capital-gate (no new gate).

**Place in the Escalation & Hardening Ladder.** The Ladder is a 5-rung
*cross-lane convention* (memory `deterministic-agents-epic`) whose
formal design the operator deferred to its own later brainstorm. This
spec is **NOT** that — it is one concrete, bounded primitive: the
per-source hold + autonomous auto-clear the data lane lacks vs DA-1.
Rung-1 detectors (selfheal, auditheal, contract-sentinel) already
ship; this consumes their escalation outcome.

Operator decisions captured (brainstorm 2026-05-17):
- Scope = the per-source hold/auto-clear primitive ONLY (full 5-rung
  Ladder = separate later brainstorm).
- The sacred whole-cycle invariant ("`DATA_OPERATIONS_COMPLETE` only
  if 100% green", CLAUDE.md "structural") is **preserved untouched**;
  the Supervisor is a per-source hold + autonomous-recovery layer, not
  a gate relaxation.
- Composition = a thin `python -m tpcore.datasupervisor` Step in
  `run_data_operations.sh` **after** Step 4/4c (Approach-A, no new
  daemon), symmetric to how selfheal/auditheal are thin callers.

## 1. Problem

DA-1 gave the *engine* lane per-engine event-sourced hold +
conservative auto-clear, so a broken engine stands down while others
keep trading and recovers autonomously. The *data* lane has no
symmetric primitive: its stand-down is the **blunt whole-cycle
freeze** — `tpcore.selfheal`/`auditheal` exit ≠0 → no
`DATA_OPERATIONS_COMPLETE` → escalation → **the operator must become
aware and intervene** for the cycle to recover. There is no tracked
per-source hold lifecycle and no autonomous auto-clear: a transiently
red source that recovers on a later cycle has no
hold→recovered record, and a chronically stuck source has no distinct
"this is not transient" escalation separate from the routine red.

`assert_passed_for_engine` (capital_gate.py:167, `ENGINE_TABLES` SoT)
already stands down only the engines that read a red source — so
per-engine granularity exists. What is missing is the **per-source
hold state machine + autonomous auto-clear** (DA-1's actual
contribution to the engine lane), data-native.

## 2. Architecture (the symmetry mapping)

| engine lane (DA-1) | Data Supervisor (data-native) |
|---|---|
| `tpcore/supervisor_state.py`: `ENGINE_HELD/CLEARED/ESCALATED/RECOVERED`; `current_hold(pool, engine)` | `tpcore/datasupervisor_state.py`: `DATA_SOURCE_HELD/CLEARED/ESCALATED/RECOVERED`; `current_source_hold(pool, source)` |
| `ops/engine_supervisor.py` `supervise()` in the engine dispatch path | `tpcore/datasupervisor.py` `datasupervise(pool, run_id)`, run by a thin `python -m tpcore.datasupervisor` Step in `run_data_operations.sh` **after** Step 4/4c |
| detect → bounded self-heal → escalate+hold | selfheal/auditheal/contract-sentinel **already** did the bounded repair in Step 4/4c; the Supervisor consumes the outcome and does **NOT** re-heal (no double-act) |
| `should_fire` `supervisor_held` pure gate | **no new gate**: the sacred whole-cycle emit invariant is untouched; existing `assert_passed_for_engine` already gates only affected engines |
| sole writer of the hold events; pure read elsewhere | `tpcore/datasupervisor.py` is the sole writer; `datasupervisor_state.current_source_hold` is the pure read for any future consumer |

Locked contract (parity with `supervisor_state` / `DATA_REPAIR_*`):
`SCHEMA_VERSION = 1`; `hold_id` = uuid4 string, sole correlation key;
NO client timestamps (DB `recorded_at` only); one-terminal liveness
(a `DATA_SOURCE_HELD` is eventually followed by exactly one
`DATA_SOURCE_CLEARED`). Event-sourced from `platform.application_log`,
**no new table / migration** (Railway-portable; restart-safe).

`current_source_hold(pool, source)`: latest `DATA_SOURCE_HELD` for
`source` whose `hold_id` has no later `DATA_SOURCE_CLEARED` — the
exact LEFT-JOIN-on-hold_id shape `supervisor_state.current_hold` uses,
keyed on `source` instead of `engine`.

## 3. Mechanism (one bounded pass per cycle, after Step 4/4c)

`datasupervise(pool, run_id)`:

1. **Compute the cycle's still-red source set**, reusing the
   *existing* red predicates (no new detection logic — same SoT the
   rung-1 agents use):
   - red `validation.%` rows → source via the selfheal HealSpec
     registry (`spec.source` for the red `check_name`);
   - red `cross_table_audit.%` rows → `<table>` component;
   - `INGESTION_FAILED` with `data->>'exception_type' =
     'AdapterContractDrift'` in the last 24h → the feed from the
     event payload.
   The union, mapped to canonical source names, is `red_sources`.
2. **Open**: for each `source ∈ red_sources` with no open hold
   (`current_source_hold` is None) → emit `DATA_SOURCE_HELD`
   `{schema:1, hold_id, source, reason}`. Idempotent: dedup on
   `(source, cycle-window)` — re-running the Step in one cycle emits
   no duplicate (same dedup discipline as `engine_dispatch`).
3. **Auto-clear** (conservative, no operator ack — DA-1 D-DA1-3
   symmetry): for each open hold whose `source ∉ red_sources` this
   cycle **AND** every latest `validation.*`/`cross_table_audit.*`
   row for that source is green (not `stale`, `confidence = 1.0`)
   with `recorded_at` strictly **after** the hold's `recorded_at`
   **AND** no `AdapterContractDrift` escalation for it since the hold
   → emit `DATA_SOURCE_CLEARED {schema:1, hold_id, source,
   clear_reason}` + `DATA_SUPERVISOR_RECOVERED {schema:1, source,
   held_cycles}`. A single not-red cycle is **insufficient** by
   itself — the green-after-hold predicate must hold (strong, not
   "seen once").
4. **Bounded escalate**: for each open hold still red after **M
   cycles** held (default `_MAX_HELD_CYCLES = 3`, counted from
   `DATA_SOURCE_HELD.recorded_at` over elapsed data-ops cycles) →
   emit `DATA_SOURCE_ESCALATED {schema:1, hold_id, source, reason,
   held_cycles}` **once** per hold (dedup on `hold_id`) — the loud
   "not transient, human required" alarm, parity with
   `DATA_REPAIR_ESCALATED`. The hold stays open (escalation ≠ clear);
   it still auto-clears later if the source genuinely recovers.

Bounded + terminating by construction: every hold ends in exactly one
`DATA_SOURCE_CLEARED` (auto-recovery) and, if it lingers, additionally
one `DATA_SOURCE_ESCALATED` (alarm) — never a silent forever-hold,
never a flapping re-heal (it does not heal).

**Crash-isolation:** any `datasupervise()` exception → structured
`datasupervisor.error` log → the Step exits **0** and the cycle
proceeds. A broken Supervisor must NEVER break the data cycle or
affect trading — the sacred emit gate (upstream, unchanged) is the
authority; this Step is state-tracking, not a gate. (Symmetric to
DA-1's "a broken supervise() never aborts dispatch".)

## 4. Composition

`run_data_operations.sh` gains one Step **after** Step 4 (selfheal) /
Step 4c (auditheal) and **before** the existing
`DATA_OPERATIONS_COMPLETE` emit logic:

```
python -m tpcore.datasupervisor   # exit 0 ALWAYS (state-tracking)
```

`tpcore/datasupervisor/__main__.py` is a thin caller: build pool, call
`datasupervise(pool, run_id)`, print a one-line summary, `return 0`
unconditionally (the only non-zero is an unbuildable pool / missing
DSN, matching the selfheal `__main__` DSN guard). It does **not**
gate; it never changes whether `DATA_OPERATIONS_COMPLETE` is emitted
(that remains exclusively the Step-4/4c 100%-green decision). Placing
it after 4/4c means the red set + escalations it reads are final for
the cycle.

## 5. Non-goals / scope boundary

- **Not** a relaxation of the whole-cycle `DATA_OPERATIONS_COMPLETE`
  invariant (sacred, structural — untouched).
- **Not** re-running self-heal / audit (selfheal/auditheal own bounded
  repair; the Supervisor consumes their outcome — no double-act).
- **Not** a new gate, **not** `risk_state` mutation (symmetric to
  D-DA1-2: hold is a tracked event-sourced fact, not a kill switch).
- **Not** touching the engine lane / `ops/engine_supervisor.py` /
  `tpcore/selfheal` / `tpcore/auditheal` / `tpcore/ingestion`
  (reciprocal of DA-1's "don't touch the data lane"). The per-source
  hold is a **forward seam**: a future engine-side change could
  consume `DATA_SOURCE_HELD` for finer `data_repair_escalated`
  granularity, and the weekly digest / dashboard could surface
  `current_source_hold` — all explicitly **out of scope** here (noted,
  not built; no consumer is wired in this spec).
- **Not** the 5-rung cross-lane Escalation & Hardening Ladder formal
  design (operator-deferred to its own brainstorm).
- **Not** a new daemon (Approach-A; DA-3-consolidation-friendly).
- Operator interaction: unchanged. This *reduces* operator toil
  (autonomous auto-clear replaces "operator must notice + unstick a
  transient red"); the loud `DATA_SOURCE_ESCALATED` still fires for a
  genuinely stuck source. The operator's approval touchpoints (Data
  Feed Change Request, weekly digest ack) are unaffected.

## 6. Phasing (each independently testable; gated PR per phase)

| Phase | Deliverable |
|---|---|
| 1 | `tpcore/datasupervisor_state.py`: `SCHEMA_VERSION`, the four event-type constants, `SourceHoldState` frozen dataclass, `current_source_hold(pool, source)` event-sourced read (LEFT-JOIN-on-hold_id, mirrors `supervisor_state.current_hold`). Fake-pool unit tests (open hold visible; cleared hold → None; newest-hold-wins; unknown source → None). **Landed dark.** |
| 2 | `tpcore/datasupervisor/` package: `datasupervise(pool, run_id)` (the §3 bounded pass — red-source detection reusing the existing red SQL, open / auto-clear / bounded-escalate, idempotent, crash-isolated internally returns a structured outcome) + `__main__.py` thin caller (exit 0 always; DSN guard). Pure: red-set + clock injected/fakeable. Deterministic fake-pool tests mirroring `test_selfheal.py`: open-on-red, idempotent-no-dup, auto-clear-only-on-strong-green, single-not-red-cycle-insufficient, bounded-escalate-at-M-once, crash-isolated. **Landed dark** (not wired). |
| 3 | Wire the thin Step into `run_data_operations.sh` after Step 4c, before the `DATA_OPERATIONS_COMPLETE` emit. Shell `bash -n` + the entrypoint import-smoke; explicit note that the emit gate is unchanged (the Step is purely additive state-tracking). |
| 4 | Docs: CLAUDE.md (the Step + that it does NOT affect the emit gate), the `deterministic-agents-epic` memory pointer (data lane now has the symmetric hold primitive), this spec → BUILT + build record. |

## 7. Open questions for the plan phase (resolve by READING code, not guessing — the auditheal/contract-sentinel discipline)

- **Cycle-window definition for dedup + the M-cycle count.** Engine
  DA-1 used cadence windows. Data has a per-cycle `run_id` /
  `application_log` `INGESTION_*`/`DATA_OPERATIONS_*` markers. The
  plan must read how a "data-ops cycle" is delimited in
  `application_log` (e.g. distinct `run_id`, or `DATA_OPERATIONS_*`
  boundaries) and define `held_cycles` as the count of *distinct
  completed data-ops cycles* since the hold's `recorded_at` — not
  wall-clock — so M is cadence-correct. Derive from the real event
  shape; do not assume.
- **Exact red-source mapping.** The plan must enumerate, from source:
  the selfheal red `_RED_SQL` + HealSpec `source` mapping; the
  auditheal `cross_table_audit.%` `_RED_SQL` + `<table>` parse; the
  `INGESTION_FAILED` `exception_type=AdapterContractDrift` payload
  field that carries the feed. Reuse the existing SQL/predicates
  verbatim (single source of truth — do not re-implement the red
  predicate; a divergence would desync the Supervisor from the gate).
- **`source` canonical-name space.** Confirm the selfheal
  `HealSpec.source`, the `cross_table_audit` `<table>`, and the
  contract-sentinel `feed` names share one canonical space (or define
  the exact normalization) so `current_source_hold(source)` keys are
  consistent across the three detectors and the future capital-gate
  `ENGINE_TABLES` consumer.
- **Package vs module.** `tpcore/datasupervisor/` package (with
  `__main__.py`) vs `tpcore/datasupervisor.py` + a `__main__` shim —
  the plan picks one mirroring how `tpcore/selfheal` / `tpcore/
  auditheal` are structured (package with `__main__.py`), for
  cross-agent symmetry.
