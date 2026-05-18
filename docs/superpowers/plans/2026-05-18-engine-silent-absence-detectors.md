# Engine Silent-Absence Detectors (#243) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]` checkboxes.

**Goal:** Build the two approved deterministic silent-absence detectors — `engine_service_sweep_silent` (a) and `engine_service_digest_stalled` (c) — in `engine_service._main_loop`, feeding the engine Ladder; record (b) trade-monitor-silent as resolved-by-existing-coverage (no new detector).

**Architecture:** Spec `docs/superpowers/specs/2026-05-18-engine-silent-absence-detectors-design.md` (v1, approved). Deterministic, NO LLM. Both checks live in the long-lived `engine_service._main_loop` 60s poll (NOT `engine_supervisor._detect_*` — that runs only inside the sweep subprocess; spec §1 fatal-objection correction). Reuse the shipped Epic-E Phase-0 patterns verbatim: `_find_new_trigger`, `_safe_emit_escalated`, `_engsvc_hold_id`, escalate-only (no `_emit_held`), `PLATFORM_SERVICE_FAILURE_CLASSES` + `DISPOSITION_POLICIES` clockwork in the same change.

**Tech Stack:** Python 3.11, asyncpg, structlog, `tpcore.calendar`, pytest (`asyncio_mode=auto`), ruff. Gated PR per phase; branch-hygiene (`git switch -c`, verify branch before commit; tests never touch the real repo).

**Reference (read, mirror — do not re-derive):** the shipped Phase-0 work in `ops/engine_service.py` (`_main_loop`, `_find_new_trigger`, `_safe_emit_escalated`, `_engsvc_hold_id`, `_maybe_fire_weekly_digest`, the `trigger_seen`/`sweep_done` markers, `POLL_INTERVAL`), `ops/engine_supervisor.py` (`PLATFORM_SERVICE_FAILURE_CLASSES`, `INFRA_FAILURE_CLASSES`, `_emit_escalated`), `ops/engine_ladder.py` (`DISPOSITION_POLICIES`, `KNOWN_ESCALATION_CLASSES`, `escalation_drift`, `EngineEscalationDisposition`), `tpcore/calendar.py` (`is_trading_day`), the existing `engine_service` + `engine_ladder` test files, the shipped Phase-0 tests as the fake-pool technique to mirror.

---

## Phase 1 — Detectors (a)+(c) + clockwork (gated PR #1)

Branch `feat/engine-silent-absence-p1` off fresh `main`.

**Files:**
- Modify: `ops/engine_service.py` (`_main_loop` — two new checks; module constants `SWEEP_SILENT_SEC`, `DIGEST_STALE_SEC`)
- Modify: `ops/engine_supervisor.py` (`PLATFORM_SERVICE_FAILURE_CLASSES` += the two classes)
- Modify: `ops/engine_ladder.py` (two `DISPOSITION_POLICIES` rows; `KNOWN_ESCALATION_CLASSES` stays lockstep via the existing union)
- Test: the existing `engine_service` test file + the `escalation_drift` clockwork test (locate first: `ls tests/test_engine_service.py scripts/tests/test_engine_*; grep -rl escalation_drift tests scripts/tests tpcore/tests`)

### Task 1.1: Clockwork-first (R2 forcing function) (TDD)
- [ ] **Step 1:** Read `ops/engine_supervisor.PLATFORM_SERVICE_FAILURE_CLASSES`, `ops/engine_ladder.{DISPOSITION_POLICIES,KNOWN_ESCALATION_CLASSES,escalation_drift,EngineEscalationDisposition}`, and the existing clockwork test that asserts `escalation_drift()` is empty.
- [ ] **Step 2 (failing test):** extend the `escalation_drift()` clockwork test to expect `engine_service_sweep_silent` + `engine_service_digest_stalled` in BOTH `PLATFORM_SERVICE_FAILURE_CLASSES` and `DISPOSITION_POLICIES`; assert `escalation_drift()` returns empty. Run → FAIL (classes absent).
- [ ] **Step 3:** Add both classes to `PLATFORM_SERVICE_FAILURE_CLASSES`; add a `DISPOSITION_POLICIES` row each — `default=EngineEscalationDisposition.STRUCTURAL`, terse rationale matching the existing Phase-0 entries' style (`engine_service_task_crashloop`/`engine_service_digest_failed`). NOT added to `INFRA_FAILURE_CLASSES`.
- [ ] **Step 4:** Run the clockwork test + `escalation_drift()` → PASS (build green).
- [ ] **Step 5:** Commit (verify branch first).

### Task 1.2: (a) `engine_service_sweep_silent` detector (TDD)
- [ ] **Step 1:** Read `_main_loop`, `_find_new_trigger` (the qualifying-trigger SQL incl. the green-`DATA_REPAIR_COMPLETE` filter), the `trigger_seen`/`sweep_done`/cursor markers, `_safe_emit_escalated`, `_engsvc_hold_id`, `POLL_INTERVAL`.
- [ ] **Step 2 (failing test, fake pool):** add `SWEEP_SILENT_SEC` module constant (`2*POLL_INTERVAL + 300`s headroom — pick the concrete int, comment why it must exceed the longest legitimate sweep). Test the new `_main_loop` check: (i) a qualifying trigger row older than `SWEEP_SILENT_SEC` with NO subsequent sweep activity ⇒ exactly one `ENGINE_ESCALATED` (`engine_service_sweep_silent`, deterministic `_engsvc_hold_id(...,"sweep")`, escalate-only, payload-parity with the shipped Phase-0 emit); (ii) NO qualifying trigger (quiet/weekend/no data-ops) ⇒ no escalation; (iii) trigger present but a sweep ran for it ⇒ no escalation; (iv) a red (non-green) `DATA_REPAIR_COMPLETE` ⇒ no escalation (already excluded by `_find_new_trigger`'s SQL — assert the check defers to it, not a reimplementation); (v) trigger younger than `SWEEP_SILENT_SEC` ⇒ no escalation (in-flight grace); (vi) re-poll after escalation ⇒ no duplicate for the same trigger/hold_id. Run → FAIL.
- [ ] **Step 3:** Implement the check in `_main_loop` (after the trigger poll), consuming the existing trigger/cursor signals — do NOT re-derive the data calendar; emit via `_safe_emit_escalated`. Crash-isolated (an emit failure must not break the loop — reuse the Phase-0 wrapping).
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit.

### Task 1.3: (c) `engine_service_digest_stalled` detector (TDD)
- [ ] **Step 1:** Read `_maybe_fire_weekly_digest` (the `digest_state["last"]` idempotence key, the day-rollover due logic, the `state["last"]==today` short-circuit), `tpcore.calendar.is_trading_day`, and how a successful `ops.weekly_digest emit` records completion to `application_log` (grep the emit/completion marker).
- [ ] **Step 2 (failing test, fake pool):** add `DIGEST_STALE_SEC` constant (≈6h; comment). Test: (i) trading day AND current ISO-week's due rollover passed by > `DIGEST_STALE_SEC` AND no successful weekly_digest completion for the current ISO-week ⇒ one `ENGINE_ESCALATED` (`engine_service_digest_stalled`, `_engsvc_hold_id(...,"weekly_digest")`, escalate-only); (ii) NOT a trading day (`is_trading_day` False — weekend/holiday) ⇒ no escalation; (iii) digest already emitted this ISO-week ⇒ no escalation; (iv) within the `DIGEST_STALE_SEC` grace ⇒ no escalation; (v) distinct from `engine_service_digest_failed` — a *failed* digest (rc≠0) path still uses the shipped class, not this one; (vi) re-poll ⇒ no duplicate. Run → FAIL.
- [ ] **Step 3:** Implement the check in `_main_loop` adjacent to the `_maybe_fire_weekly_digest` call; `tpcore.calendar.is_trading_day` guard; ISO-week scoping; emit via `_safe_emit_escalated`; crash-isolated.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit.

### Task 1.4: Phase-1 verify + PR
- [ ] **Step 1:** Full suite `python -m pytest tpcore/tests/ tests/ scripts/tests/ -q 2>&1 | tail -3` → 0 failed, ≥ baseline + new; `ruff check ops/engine_service.py ops/engine_supervisor.py ops/engine_ladder.py <test files>` clean (no new noqa beyond the documented Phase-0 BLE001 precedent); `git -C <repo> branch --list 'llm-triage/*'` empty / no real-repo mutation; `git diff --stat` = only the Phase-1 files. `scripts/tests/test_two_daemon_invariant.py` green unedited (no new daemon/co-task).
- [ ] **Step 2:** Push, gated PR, wait for the CI run to register then `gh run watch --exit-status`, squash-merge `--delete-branch`, sync `main`.

---

## Phase 2 — Docs reconciliation (gated PR #2)

Branch `docs/engine-silent-absence-p2` off fresh `main`.

**Files:** `docs/superpowers/specs/2026-05-18-engine-llm-triage-advisory-layer-design.md` (§7a detector-home correction + (b) resolved note), `docs/ENGINE_ESCALATION_HARDENING_LADDER.md` (R1 coverage note), `docs/superpowers/specs/2026-05-18-engine-silent-absence-detectors-design.md` (this spec → BUILT + build record), memory `project_engine_llm_triage_ownership.md` (#243 → done, with the (b) resolution; outside the repo, not in the commit).

- [ ] **Step 1:** Correct Epic-E spec §7a: detector home = `engine_service._main_loop` (not `engine_supervisor._detect_*` — explain the supervise()-runs-in-sweep reason); record (b) trade-monitor-silent as **resolved-by-existing-coverage** (the `daemon_heartbeats` substrate + dashboard probe + `engine_service_task_crashloop`), NOT an open gap; (a)+(c) BUILT. Accuracy-discipline: cross-check every claim against merged Phase-1 code; no overclaim.
- [ ] **Step 2:** `docs/ENGINE_ESCALATION_HARDENING_LADDER.md`: extend the R1 coverage note — engine-daemon sweep-silent / digest-stalled now escalate deterministically (same doc convention).
- [ ] **Step 3:** This spec `Status:` → `BUILT 2026-05-18` + Build record (P1 #<n>, P2 #<this>).
- [ ] **Step 4:** `git diff --stat` = docs only; collection clean; gated PR; CI-green; squash-merge; sync. Then update memory `project_engine_llm_triage_ownership.md` (#243 → done + the (b) resolution) + the `MEMORY.md` index line (re-read MEMORY.md first — parallel session edits it; touch only the relevant line).

---

## Self-Review

**1. Spec coverage:** §1 detector-home correction → Phase 1 (`_main_loop`, not supervisor) + Phase 2 §7a fix; §2(a) sweep-silent → Task 1.2 (predicate, anti-false-positive via consuming existing triggers, bound, class/disposition/hold_id, location); §2(b) deferred-resolved → Phase 2 doc record (no code — correct); §2(c) digest-stalled → Task 1.3 (distinct from digest_failed, is_trading_day guard, ISO-week); §3 clockwork → Task 1.1 (R2-forced, same PR); §4 non-goals (no new daemon/co-task, deterministic, no calendar re-derivation) → enforced in 1.2/1.3 + the topology-test-unedited check; §5 phasing → Phases 1–2. ✓

**2. Placeholder scan:** the only "pick the concrete int" items (`SWEEP_SILENT_SEC`, `DIGEST_STALE_SEC`) are explicit, bounded, commented-rationale decisions in their tasks (≈1800s / ≈6h with the stated invariants), not TBDs. Every task has files + failing-test + impl + verify + commit.

**3. Type/name consistency:** class names `engine_service_sweep_silent` / `engine_service_digest_stalled` identical across Tasks 1.1/1.2/1.3 and the clockwork; `_engsvc_hold_id`/`_safe_emit_escalated`/`_find_new_trigger` reused verbatim (shipped Phase-0 symbols); `STRUCTURAL` disposition both; both escalate-only (no `_emit_held`), matching the shipped Phase-0 pattern.

Execution: subagent-driven-development — fresh implementer per task-group, split spec-then-code-quality reviews, gated PR per phase, CI-green before merge, branch-hygiene before every commit.
