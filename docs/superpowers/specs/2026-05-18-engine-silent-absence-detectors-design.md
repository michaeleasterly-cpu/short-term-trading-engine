# Engine Silent-Absence Detectors (#243) — Design **v1 (expert-scoped)**

**Status:** spec **v1 (expert-scoped)** 2026-05-18 (ENGINE lane,
deterministic — NO LLM). Brainstorm-by-investigation (code-grounded
expert scoping) → **spec (this doc)** → operator spec-review gate →
plan → phased subagent build. Closes the deferred Epic-E Phase-0
follow-up **#243**; companion of `docs/superpowers/specs/
2026-05-18-engine-llm-triage-advisory-layer-design.md` §7a.

**Ownership:** data-lane session (owns Epic E + its deferred
follow-ups). Engine-lane session notified (collision-avoidance on
`ops/engine_service.py` / `ops/engine_supervisor.py` /
`ops/engine_ladder.py`).

## 0. What this is

Deterministic DA-class detectors that emit `ENGINE_ESCALATED` when a
co-hosted engine-daemon platform service is **alive but silently not
doing its job** (no crash — the crash case shipped in Epic-E Phase 0
as `engine_service_task_crashloop` / `engine_service_digest_failed`).
The LLM is **never** the detector; these are deterministic and feed
the engine Ladder exactly like the shipped Phase-0 emitters, after
which `engine_ladder.list_undispositioned()` (and thus the engine
LLM-triage agent) sees them with **zero predicate change**.

## 1. SPEC CORRECTION (a fatal objection the scoping found — carry it)

Epic-E spec §7a's frozen table states the deferred detectors "live in
new `engine_supervisor._detect_*`". **That is structurally wrong
against the code and is corrected here.** `engine_supervisor.
supervise()` is invoked *inside the sweep subprocess*
(`ops/engine_dispatch.py` `_safe_supervise` in `dispatch_once` ←
`scripts/run_all_engines.sh` ← `engine_service._run_engine_sweep`
`subprocess.run`). It only runs **when a sweep runs**, per-roster-
engine. A "the sweep never ran" detector placed there is dead code
(if the sweep didn't run, the detector didn't run). **Therefore all
silent-absence detection lives in the long-lived
`engine_service._main_loop` poll (60s)** — the only context that
fires independently of the sweep. The #243 build updates Epic-E spec
§7a's "detector home" wording accordingly (P4 doc step).

## 2. Frozen scope (per detector — build to this verbatim)

### (a) `engine_service_sweep_silent` — **BUILD**
- **Predicate (deterministic):** a qualifying trigger landed but no
  sweep ran. Reuse the existing deterministic substrate:
  `engine_service._find_new_trigger` already isolates the newest
  `DATA_OPERATIONS_COMPLETE` / green-`DATA_REPAIR_COMPLETE` >
  cursor; `trigger_seen` / `sweep_done` markers already exist. FIRE
  iff: a qualifying trigger row's `recorded_at` is older than
  `SWEEP_SILENT_SEC` AND no subsequent sweep activity
  (`engine_dispatch` startup / `SCAN_COMPLETE`) for it.
- **`SWEEP_SILENT_SEC` bound:** `2 × POLL_INTERVAL` + sweep-duration
  headroom (concrete value frozen in the plan's task; ≈1800s — must
  be > the longest legitimate sweep so an in-flight long sweep can
  never look missed; the synchronous-sweep + cursor-only-advances-
  after-return invariant already guarantees this).
- **Anti-false-positive (all already deterministic):** (1) only
  counts when a qualifying trigger exists — a quiet weekend / non-
  trading day / held data lane emits NO trigger, so there is nothing
  to be late for (the data calendar is **consumed via the existing
  trigger events, never re-derived** — satisfies the cross-lane
  no-reimplementation constraint); (2) the green-`DATA_REPAIR_
  COMPLETE` filter already in `_find_new_trigger`'s SQL excludes a
  red repair; (3) suppress while a sweep is in-flight.
- **Class:** `engine_service_sweep_silent` (added to
  `PLATFORM_SERVICE_FAILURE_CLASSES`). **Disposition:** `STRUCTURAL`
  (a trigger that produced no sweep is a dispatch/daemon defect —
  same family as the shipped Phase-0 classes). **Escalate-only**, NO
  `_emit_held`. **hold_id:** reuse `engine_service._engsvc_hold_id(
  "engine_service_sweep_silent", "sweep")` verbatim (stable per
  fault identity).
- **Location/cadence:** new check in `_main_loop` after the trigger
  poll, emitting via the existing `_safe_emit_escalated`; 60s.

### (b) trade-monitor-silent — **STILL-DEFER (resolved by existing coverage — NOT a gap, NOT a new detector)**
- The Phase-0 expert assumed (b) needed a new `TradeMonitor`
  stream-heartbeat substrate. **Refuted by the code:** `tpcore/
  trade_monitor._heartbeat_writer` ALREADY UPSERTs
  `platform.daemon_heartbeats` every 900s with a `healthy/degraded`
  status, and a deterministic consumer ALREADY exists (the dashboard
  `trade_monitor_heartbeat` `--check` probe, 60-min staleness). A
  wedged-but-connected stream is already covered by that
  heartbeat-`degraded` + probe + the shipped
  `engine_service_task_crashloop`.
- "No trade-updates for T while market open" has **no crisp
  non-flaky predicate**: zero orders is the *normal* state (no
  engine has graduated the DSR gate; only canary's 1-share SPY,
  whose updates don't match an `open_orders` row). Silence ≠ fault.
- **Decision: do NOT build a new detector.** Building one would
  duplicate an existing deterministic detector and risk
  false-positives on the LIVE trade daemon for zero coverage gain.
  #243 records this as **resolved-by-existing-substrate**, not an
  open gap. (A partial honest delivery beats a flaky live
  escalation — the deferral rationale of Phase-0, upheld with
  evidence.)

### (c) `engine_service_digest_stalled` — **BUILD**
- **Distinct from the shipped `engine_service_digest_failed`** (which
  fires on digest spawn-exception / rc≠0). (c) covers the case where
  `_maybe_fire_weekly_digest` was **never reached / never advanced**
  (e.g. the sweep co-task wedged, or the daemon respawned mid-week
  with `digest_state` reset and the `state["last"]==today`
  short-circuit suppressing catch-up).
- **Predicate (deterministic):** `tpcore.calendar.is_trading_day(now)`
  (the anti-false-positive guard — no digest is due on a
  weekend/holiday) AND the current ISO-week's due rollover has passed
  by > `DIGEST_STALE_SEC` (bounded, ≈6h, frozen in the plan) AND no
  successful `ops.weekly_digest emit` completion marker for the
  current ISO-week exists in `application_log`.
- **Class:** `engine_service_digest_stalled` (→
  `PLATFORM_SERVICE_FAILURE_CLASSES`). **Disposition:** `STRUCTURAL`
  (same family as `engine_service_digest_failed`). Escalate-only, no
  `_emit_held`. **hold_id:** `_engsvc_hold_id(
  "engine_service_digest_stalled", "weekly_digest")`.
- **Location/cadence:** new check in `_main_loop` adjacent to the
  existing `_maybe_fire_weekly_digest` call; 60s.

## 3. Mandatory clockwork (R2 forcing function)

Add `engine_service_sweep_silent` + `engine_service_digest_stalled`
to `engine_supervisor.PLATFORM_SERVICE_FAILURE_CLASSES` **and** add a
`DISPOSITION_POLICIES` row each (both `STRUCTURAL`) in the **same
PR** — `engine_ladder.escalation_drift()` fails the build otherwise.
NOT added to `INFRA_FAILURE_CLASSES` (keeps `_auto_clear` correctly
inert; matches the shipped Phase-0 pattern). They flow into
`list_undispositioned()` with zero predicate change.

## 4. Non-goals / scope

- Deterministic only. The LLM is never the detector / never in this
  path. No new daemon, no new co-task (both checks are in the
  existing `_main_loop`); `test_two_daemon_invariant.py` untouched.
- Not in any trading/risk path — pure read + advisory escalate in
  the poll co-task, crash-isolated from the monitor co-task.
- (b) is explicitly NOT built (resolved by existing coverage).
- No re-derivation of the data-lane calendar — (a) consumes the
  existing completion events; (c) uses only `tpcore.calendar`
  trading-day.

## 5. Phasing (gated PR per phase; subagent-driven)

| Phase | Deliverable |
|---|---|
| 1 | **(a) sweep-silent + (c) digest-stalled detectors + clockwork**, all in `engine_service._main_loop` reusing `_find_new_trigger`/`_safe_emit_escalated`/`_engsvc_hold_id`; the two `PLATFORM_SERVICE_FAILURE_CLASSES` + `DISPOSITION_POLICIES` rows in the SAME change (R2-forced); TDD with fake-pool tests proving the predicate fires on genuine silence AND does NOT false-positive on (quiet weekend / no trigger / red repair / in-flight sweep / non-trading-day). One gated PR. |
| 2 | **Docs reconciliation:** correct Epic-E spec §7a "detector home" (→ `engine_service._main_loop`, not `engine_supervisor._detect_*`); record (b) as resolved-by-existing-substrate; `docs/ENGINE_ESCALATION_HARDENING_LADDER.md` R1 coverage note; this spec → BUILT + build record; close #243; update memory `project_engine_llm_triage_ownership` (#243 → done, with the (b) resolution). One gated PR. |

## 6. Resolved questions (read, not guessed)

- Detector home: `engine_service._main_loop` (the §7a fatal-objection
  correction). The `supervise()`-runs-in-sweep finding is the
  evidence.
- (b): resolved by the EXISTING `daemon_heartbeats` substrate +
  dashboard probe + `engine_service_task_crashloop` — no new
  detector (evidence-based, conservative — a flaky live-daemon
  escalation is worse than honest non-build).
- `hold_id` / escalate-only / clockwork / disposition: reuse the
  shipped Phase-0 patterns verbatim.

**Spec ready for the operator review gate** (it corrects the prior
Epic-E §7a detector-home, and consciously delivers 2-of-3 with (b)
explicitly resolved-by-existing-coverage, not silently dropped).
