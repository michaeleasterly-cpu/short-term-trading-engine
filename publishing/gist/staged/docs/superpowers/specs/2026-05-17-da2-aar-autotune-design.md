# DA-2 — AAR Auto-Tune — Design

**Status:** approved 2026-05-17 (operator; trigger policy delegated to
expert → Option-A-refined; clear policy + Approach A approved; design
approved). Sub-project DA-2 of the **Deterministic Agents epic**
(DA-1 Supervisor ✅ #27 → **DA-2 AAR Auto-Tune** → DA-3 Two-Daemon
Consolidation). Builds on DA-1's merged `ENGINE_HELD`/`ENGINE_CLEARED`
primitive.

## 1. Problem

`tpcore/forensics` writes per-engine behavioral triggers to
`platform.forensics_triggers` daily (`outlier_loss`, `loss_cluster`,
`drawdown_period`; fingerprint-deduped; operator-set `resolved_at`).
**Nothing autonomously acts on them** — they sit until the operator
reviews the dashboard. The aar→engine control path (the third lane's
loop) is open. DA-1 deliberately scoped behavioral OUT and left the
reusable hold primitive for DA-2.

## 2. Architecture

A new `ops/aar_autotune.py` — a bounded, deterministic agent that
reads `platform.forensics_triggers` and applies a fixed policy by
**reusing DA-1's `ENGINE_HELD`/`ENGINE_CLEARED` tpcore primitive**
(`tpcore.supervisor_state`), NOT the DA-1 module. Per dispatch actor,
invoked by `ops/engine_dispatch.py` via a new `_safe_autotune(pool,
engine, now)` (mirrors `_safe_supervise` — call-site crash isolation)
after `_safe_supervise` and before `_dispatch_engine`. No new daemon
(advances toward DA-3). Infra-supervisor (self-heal, auto-clear) and
behavioral-autotune (no self-heal, operator-clear) are distinct
single-purpose modules on the shared `application_log` bus — mirrors
the data lane's `data_repair_service` vs `cutover_agent` split.
Crash-isolated: a broken autotune must NEVER abort the sweep or block
trading (same invariant as DA-1/allocator).

## 3. Decision logic (expert Option-A-refined, deterministic)

Read OPEN (`resolved_at IS NULL`) `forensics_triggers` rows for the
engine. Single knob `LOSS_CLUSTER_HOLD_LEN = 5` (module constant,
`ENGINE_AUTOTUNE_LOSS_CLUSTER_HOLD_LEN` env-overridable):

- `outlier_loss` (a single 3σ-tail trade) → **ESCALATE-only**, never
  hold. One fat-tail loss carries ~zero edge-decay information.
- `loss_cluster` with `payload->>'streak_length'` 3–4 →
  **ESCALATE-only**. Routine for a sub-55%-hit-rate daily strategy.
- `loss_cluster` with `streak_length >= LOSS_CLUSTER_HOLD_LEN` (≥5) →
  **HOLD + ESCALATE**. ~3% tail of the streak distribution =
  structurally cold.
- `drawdown_period` (unresolved; its 10%/14d threshold is already
  enforced by the forensics producer — DA-2 does not re-check it) →
  **HOLD + ESCALATE**. Sustained drawdown is the real regime-break
  signal.

Every HOLD also emits `ENGINE_ESCALATED` (no silent stand-down).
Deterministic, bounded, idempotent. **Rationale (expert):** every
engine fails the DSR≥0.95/credibility≥60 gate, so capital
preservation strictly dominates edge-capture on real decay signals
(`loss_cluster≥5`, `drawdown_period`), while noise signals
(`outlier_loss`, short clusters) must never churn the engine.

## 4. The behavioral hold

Emit `ENGINE_HELD` with `failure_class = "behavioral"` (a SINGLE
class; `reason` carries the trigger kind + the causing
`fingerprint`(s)), plus a `triggers` list of causing fingerprints in
the payload for **audit/traceability** (the clear predicate is §5's
re-evaluation, NOT a fingerprint match — see §5). DA-2 has its own
thin emitters that
mirror the locked `application_log` INSERT
(`(engine, run_id, event_type, severity, message, data::jsonb)`,
`json.dumps(payload)`, DB `recorded_at`, schema:1) — the same
"mirror the locked INSERT" convention DA-1 used; no `ops`→`ops`
import of `engine_supervisor`. `should_fire`'s existing
`supervisor_held` check (via `tpcore.supervisor_state.current_hold`)
enforces the stand-down **for free** — zero gate/engine changes.

## 5. Clear path — operator-only (expert, D-D2-3)

Each cycle, if the engine has an uncleared `failure_class=
"behavioral"` hold (`current_hold`), DA-2 **re-evaluates the §3 HOLD
condition against currently-open triggers**: clear iff the engine has
ZERO open (`resolved_at IS NULL`) HOLD-eligible triggers remaining —
i.e. no `drawdown_period` and no `loss_cluster` with `streak_length
>= LOSS_CLUSTER_HOLD_LEN` is still unresolved. If so → emit
`ENGINE_CLEARED` (hold_id from the hold). This re-evaluation (rather
than matching the originally-recorded fingerprints) is deliberate: it
keeps the engine held if a NEWER hold-eligible trigger fired after the
hold (the one-hold rule §6 means DA-2 never emitted a second hold for
it), so resolving only the original trigger cannot prematurely resume
a still-decayed engine. The recorded payload `triggers` list is for
audit only. **No cooldown / no auto-condition-gone** — a behaviorally
decayed marginal engine must not auto-resume; the human reviews the
Sprint Dossier and resolves the trigger. The operator resolves via
the EXISTING dashboard `resolved_at` UPDATE — DA-2 adds no new
operator surface. This is a deliberate, expert-justified divergence
from DA-1's clean-cycle auto-clear (infra failures self-recover;
edge-decay needs judgment); consistent with the platform's existing
operator-ack philosophy (weekly_digest live-clearance, dashboard
resolved_at).

## 6. Integration seam guard (required — the one DA-1 touch)

`tpcore.supervisor_state.current_hold` returns the latest uncleared
`ENGINE_HELD` for an engine REGARDLESS of `failure_class`. DA-1's
`engine_supervisor._detect_and_act` does `hold = await current_hold();
if hold is not None: await _auto_clear(...); return`. For a
`behavioral` hold, DA-1's `_auto_clear` clean-cycle predicate would
WRONGLY emit `ENGINE_CLEARED` on one clean cycle, auto-resuming a
behaviorally-stood-down engine. **Guard:** `engine_supervisor.
_auto_clear` early-returns unless `hold.failure_class` is in DA-1's
infra set (`{"crashed_startup","scheduler_crash",
"data_request_timeout","data_repair_escalated","missed_cycle"}`) — it
only clears what it created. Behavioral holds are owned solely by
DA-2's operator-resolved clear. This is the ONLY change to the DA-1
module — a small surgical early-return at the seam; behavior-preserving
for all infra classes (DA-1's suite is the oracle).

**Cross-agent hold partition (net effect):** at most one uncleared
`ENGINE_HELD` per engine. If DA-1 supervise placed an infra hold this
cycle, DA-2 (running after, same cycle) sees `current_hold != None`
and does NOT place a behavioral hold (one-hold rule: DA-2 skips
hold-emission entirely when ANY uncleared hold exists; it only
attempts a behavioral CLEAR when the existing hold's class ==
"behavioral"). Symmetrically DA-1 leaves behavioral holds alone (the
guard). Whichever agent owns the active hold's `failure_class` owns
its clear.

## 7. Composition / wiring

`ops/engine_dispatch.py` `dispatch_once`, per actor (allocator first,
then ROSTER), in order: `await _safe_supervise(pool, engine, now,
invoke)` → `await _safe_autotune(pool, engine, now)` → `await
_dispatch_engine(pool, now, engine, invoke)`. `_safe_autotune` mirrors
`_safe_supervise` exactly (try/except → `engine_dispatch.
autotune_failed` log → proceed; never abort). `aar_autotune.autotune`
is ALSO internally crash-isolated (defense in depth, like DA-1). The
allocator is an actor with no trades → no `forensics_triggers` → DA-2
no-ops for it (uniform, harmless). DA-2 needs no invoker (behavioral
holds have no self-heal — D-D2 mandate), so `_safe_autotune` has no
`invoke` parameter.

## 8. Idempotency / bounded / Railway

- Idempotency: one uncleared `ENGINE_HELD` per engine is the dedup —
  a re-run with an already-behavioral-held engine sees the hold and
  only attempts the operator-resolved clear; a HOLD-eligible trigger
  that already produced a hold cannot double-emit (the one-hold rule
  + `current_hold`). A second same-cycle `autotune` is structurally
  impossible (single `_safe_autotune` call per actor; engine-service
  cursor prevents same-cycle `dispatch_once` re-runs).
- Bounded: pure DB reads + at most one `ENGINE_HELD`/`ENGINE_ESCALATED`
  /`ENGINE_CLEARED` emit per engine per cycle. No loops, no retries.
- Railway: event-sourced (`application_log` + read-only
  `forensics_triggers`), no new table/migration, restart-safe,
  deployment-agnostic.

## 9. Error handling

`autotune` fully crash-isolated (internal `try/except Exception`
→ structured `aar_autotune.error` log → return) AND `_safe_autotune`
call-site isolated (defense in depth, the DA-1 `_safe_supervise`
pattern). The sweep proceeds for every actor regardless. Everything
deterministic + bounded (no unbounded queries — newest-N or
fingerprint-keyed reads).

## 10. Testing

Unit (fake pool, queued `forensics_triggers`/`application_log` rows,
no real DB):
- `outlier_loss` open → `ENGINE_ESCALATED` only, NO `ENGINE_HELD`.
- `loss_cluster` streak 3 and 4 → ESCALATE-only, no hold.
- `loss_cluster` streak ≥ `LOSS_CLUSTER_HOLD_LEN` → `ENGINE_HELD` +
  `ENGINE_ESCALATED`, failure_class "behavioral", reason carries kind
  + fingerprint.
- `drawdown_period` open → `ENGINE_HELD` + `ENGINE_ESCALATED`.
- behavioral-held + ≥1 causing trigger still `resolved_at IS NULL` →
  NO `ENGINE_CLEARED`.
- behavioral-held + ALL causing triggers resolved AND no other open
  HOLD-eligible trigger → exactly one `ENGINE_CLEARED` (correct
  hold_id, schema:1).
- behavioral-held + original causing trigger resolved BUT a newer
  unresolved HOLD-eligible trigger exists (e.g. a fresh
  `drawdown_period`) → NO `ENGINE_CLEARED` (§5 re-evaluation keeps it
  held; guards against premature resume).
- engine already INFRA-held (`current_hold` failure_class ≠
  "behavioral") → DA-2 emits nothing (one-hold rule).
- no open triggers → no events.
- crash-isolation: a raising `autotune`/`_safe_autotune` does NOT
  abort `dispatch_once` (every actor still dispatched).
- seam guard: `engine_supervisor._auto_clear` with a `behavioral`
  hold + a clean cycle → does NOT emit `ENGINE_CLEARED` (DA-1 leaves
  it); with each infra class + clean cycle → still clears (DA-1 suite
  is the behavior-preserving oracle).
- `should_fire` returns no-fire for a behavioral-held engine (reuses
  the existing `supervisor_held` gate — assert end-to-end).
- wiring: `_safe_autotune` called once per actor, AFTER
  `_safe_supervise`, BEFORE `_dispatch_engine`; B/C/DA-1 dispatch +
  supervisor suites green.

## 11. Scope boundary

DA-2 delivers: `ops/aar_autotune.py` (read forensics → policy → emit
held/escalated/cleared, crash-isolated), the `_safe_autotune` wiring
in `ops/engine_dispatch.py`, the single `engine_supervisor._auto_clear`
infra-class guard, the `LOSS_CLUSTER_HOLD_LEN` constant, and tests.
DA-2 does **NOT**: modify the forensics producer or its thresholds
(`tpcore/forensics/*` — DA-2 only reads its output table), touch the
data lane (`tpcore/selfheal`, `tpcore/feeds`, `tpcore/ingestion`,
`ops/data_repair_service.py`, `ops/cutover_agent.py`,
`ops/weekly_digest.py`), change allocation/risk/sizing logic, alter
DA-1's infra detection/self-heal, add a new operator surface (reuses
dashboard `resolved_at`), or change `tpcore.supervisor_state`
/`should_fire` (the gate already enforces any `ENGINE_HELD`). DA-3
consolidates daemons later. Acceptance: the exact §3 mapping; behavioral
holds enforced via the existing gate; operator-only clear; the seam
guard makes DA-1 ignore behavioral holds (DA-1 suite green); ≤1 hold
per engine; full suite + ruff + check_imports green; no data-lane file
touched.

## 12. Decisions log

- **D-D2-1** Trigger policy = **expert Option-A-refined** (§3):
  outlier_loss & loss_cluster<5 ESCALATE-only; loss_cluster≥5 &
  drawdown_period HOLD+ESCALATE; every HOLD also ESCALATEs.
- **D-D2-2** Reuse DA-1's `ENGINE_HELD`/`ENGINE_CLEARED` tpcore
  primitive; behavioral logic in a SEPARATE `ops/aar_autotune.py`
  (Approach A) — distinct lifecycle ⇒ distinct module.
- **D-D2-3** **Operator-only clear** via the existing
  `forensics_triggers.resolved_at` (expert) — deliberate divergence
  from DA-1's auto-clear; no auto-resume of a decayed engine.
- **D-D2-4** Single `failure_class = "behavioral"`; `reason` + a
  payload `triggers` fingerprint list are AUDIT-only. The clear
  predicate is §5's re-evaluation of the §3 HOLD condition against
  currently-open triggers (NOT a fingerprint match) — safe against
  newer hold-eligible triggers firing after the hold.
- **D-D2-5** `engine_supervisor._auto_clear` gains an infra-class
  guard (early-return for non-infra `failure_class`) — the only DA-1
  touch; behavior-preserving for infra.
- **D-D2-6** `LOSS_CLUSTER_HOLD_LEN = 5` single deterministic knob;
  the forensics producer's emit-at-3 behavior is UNTOUCHED (DA-2 only
  reads; ≥5 is a DA-2-side gate on the read).
