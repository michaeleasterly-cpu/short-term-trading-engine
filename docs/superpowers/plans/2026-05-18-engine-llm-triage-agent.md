# Engine-Lane LLM Triage Agent (Epic E / Engine Ladder R5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]` checkboxes.

**Goal:** Build the engine-lane LLM-triage advisory agent at parity with the shipped data-lane #187, covering the full engine lane *and* the platform services co-hosted in the engine daemon, with a deterministic detection-gap closure as the prerequisite Phase 0.

**Architecture:** Spec `docs/superpowers/specs/2026-05-18-engine-llm-triage-advisory-layer-design.md` (v1.1, approved). Phase 0 is deterministic (no LLM) — it makes engine-daemon platform-service failures escalate into the engine Ladder so `engine_ladder.list_undispositioned()` is a *complete* surface. Phases 1–4 mirror #187: reuse the shipped pure `tpcore.llm_data_triage.{fence,canary}` + the `ops.llm_data_triage` Anthropic wrapper *verbatim* (provenance baseline injected by arg — no package rename), engine-native `select`/`packet`/persona, B1 daemon placement (co-host in the existing process-isolated `ops/llm_triage_service.py`), label-gated CI fence, gated PR per phase, CI-green before merge.

**Tech Stack:** Python 3.11, asyncpg, structlog, pydantic v2, official `anthropic` SDK (mocked in CI), pytest (`asyncio_mode=auto`), ruff. Gated PRs; branch-hygiene (`git switch -c`, verify `git branch --show-current` before every commit; tests never touch the real repo — `feedback_git_hygiene_method`).

**Reference artifacts to mirror (read, do not re-derive):** `ops/llm_data_triage.py`, `ops/llm_triage_service.py`, `tpcore/llm_data_triage/{__init__,select,packet,fence,canary}.py`, `scripts/llm_triage_pr_check.py`, `docs/llm_data_triage_persona.md`, `.github/workflows/ci.yml` (`llm-triage-fence` job), `scripts/tests/test_two_daemon_invariant.py`, `scripts/llm_triage_pr_check.py` (#63-hardened worktree handling). Engine integration: `ops/engine_ladder.py`, `ops/engine_supervisor.py`, `ops/aar_autotune.py`, `ops/engine_service.py`, `tpcore/supervisor_state.py`.

---

## Phase 0 — Deterministic detection-gap closure (NO LLM; prerequisite; gated PR #1)

Branch `feat/engine-triage-p0` off fresh `main`.

**Files:**
- Modify: `ops/engine_service.py` (the `_run_supervised` co-task crash path + the swallowed weekly-digest-failure path)
- Modify: `ops/engine_supervisor.py` (`INFRA_FAILURE_CLASSES` / a sibling platform-service class set; the `_emit_escalated` reuse)
- Modify: `ops/engine_ladder.py` (`DISPOSITION_POLICIES` rows for the new class(es); `KNOWN_ESCALATION_CLASSES` stays lockstep)
- Modify: `docs/ENGINE_ESCALATION_HARDENING_LADDER.md` (R1 coverage note: platform-service escalations)
- Test: `tests/test_engine_service.py` (or the existing engine_service test — locate first), `tpcore/tests/test_engine_ladder*.py` / the `escalation_drift` clockwork test

### Task 0.1: Plan-time expert sub-pass — bound the silent-absence detection scope
- [ ] **Step 1:** Dispatch an expert subagent (read-only) to decide, against real code, the exact bounded scope of Phase-0 detection: (a) crash-loop emitter in `_run_supervised` (recurrence threshold/budget) — definitely in; (b) swallowed weekly-digest-failure emitter — definitely in; (c) silent-absence detectors (sweep produced no trigger in N windows / no trade-updates streamed / digest idempotence-key stalled) — decide which are deterministic+bounded+testable enough to include now vs defer, and where they live (DA-1 `engine_supervisor` detection vs `engine_service`). Output: the frozen Phase-0 deliverable list + the new `failure_class` name(s). Record the decision in the spec §7a (append "Phase-0 scope frozen:" note) and this task list.
- [ ] **Step 2:** Controller reviews the expert output; lock the class name(s) (e.g. `engine_service_task_crashloop`, `engine_service_silent_<task>`), the recurrence budget, and the detector location. No code yet.

### Task 0.2: Crash-loop escalation emitter (TDD)
- [ ] **Step 1:** Read `ops/engine_service.py` `_run_supervised` (the `engine_service.task_crashed` log + 5s-backoff restart) and `ops/engine_supervisor.py` `_emit_escalated` (payload `{schema, hold_id, engine, failure_class, reason, attempts}` + its `_INSERT_SQL` into `platform.application_log`).
- [ ] **Step 2 (failing test):** in the engine_service test file, a test that drives a co-task that crashes repeatedly past the locked recurrence budget and asserts an `ENGINE_ESCALATED` row is emitted (fake pool/log) with the new `failure_class`, a stable `hold_id` (deterministic from task name, so dedup works), `engine="<platform-service:taskname>"`, and the payload shape byte-mirroring `engine_supervisor._emit_escalated`. Assert a single crash (within budget) does NOT escalate (still just logs+restarts). Run → FAIL.
- [ ] **Step 3:** Implement: a bounded restart counter per task in `_run_supervised`; on exceeding the budget, emit `ENGINE_ESCALATED` via the *reused* emit helper (import/call `engine_supervisor._emit_escalated` or factor a shared `_emit_engine_escalated` if cleaner — do NOT re-author the SQL). Crash-isolated: the emitter failing must not kill the daemon (log + continue restart). Keep the existing log+restart behavior intact below budget.
- [ ] **Step 4:** Run the test → PASS. Run the full engine_service test module.
- [ ] **Step 5:** Commit (verify branch first).

### Task 0.3: Swallowed weekly-digest-failure emitter (TDD)
- [ ] **Step 1:** Read the weekly-digest trigger path (`_maybe_fire_weekly_digest` — the `except … logger.error … return` that swallows a non-zero `python -m ops.weekly_digest emit` subprocess).
- [ ] **Step 2 (failing test):** assert that a non-zero digest subprocess exit emits `ENGINE_ESCALATED` (new platform-service class, stable `hold_id`) instead of being silently swallowed; a success path emits nothing. Run → FAIL.
- [ ] **Step 3:** Implement the emitter on the failure branch (reuse the same emit helper); preserve "never raises out of the digest path" (still returns; just escalates first). Crash-isolated.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit.

### Task 0.4: Register class(es) + DISPOSITION_POLICIES in the SAME change (R2 forcing function)
- [ ] **Step 1:** Read `ops/engine_supervisor.INFRA_FAILURE_CLASSES`, `ops/engine_ladder.{KNOWN_ESCALATION_CLASSES,DISPOSITION_POLICIES,escalation_drift}` and the clockwork test that asserts `escalation_drift()` is empty.
- [ ] **Step 2 (failing test):** extend the `escalation_drift()` clockwork test to expect the new platform-service class(es) present in BOTH the known set and `DISPOSITION_POLICIES`; assert `escalation_drift()` returns empty (no missing/extra). Run → FAIL (class not yet added).
- [ ] **Step 3:** Add the new class(es) to the known set + a `DISPOSITION_POLICIES` row each (disposition value chosen from the existing `EngineEscalationDisposition` enum — `structural` is the likely default for a platform-service crash-loop; the expert sub-pass in 0.1 fixes the exact value + rationale). NO new enum member.
- [ ] **Step 4:** Run the clockwork test + `escalation_drift()` → PASS (build no longer red).
- [ ] **Step 5:** Commit.

### Task 0.5: Ladder doc + Phase-0 PR
- [ ] **Step 1:** Update `docs/ENGINE_ESCALATION_HARDENING_LADDER.md` R1 coverage to note engine-daemon platform-service escalations now flow in (same doc convention).
- [ ] **Step 2:** Full suite green; ruff clean; `git diff --stat` = only the Phase-0 files. Push, gated PR, CI-green, squash-merge (`gh pr merge --squash --delete-branch`), sync `main`.

---

## Phase 1 — Safety skeleton, no LLM, dark (gated PR #2)

Branch `feat/engine-triage-p1` off fresh `main` (post-Phase-0).

**Files:**
- Create: `tpcore/engine_llm_triage/__init__.py` (`PERSONA_VERSION`), `tpcore/engine_llm_triage/select.py`, `tpcore/engine_llm_triage/packet.py`
- Create: `docs/engine_llm_triage_persona.md`
- Modify: `tpcore/llm_data_triage/fence.py`, `tpcore/llm_data_triage/canary.py` (additive: provenance baseline accepted as an injected argument — default preserves current data-lane behavior, byte-no-op for #187)
- Test: `tpcore/tests/test_engine_llm_triage_{select,packet,persona}.py`, regression-run `tpcore/tests/test_llm_data_triage_{fence,canary}.py`

### Task 1.1: Parameterise the shipped pure fence/canary (additive, data-lane no-op)
- [ ] **Step 1:** Read `tpcore/llm_data_triage/fence.py` (`hard_denied_paths`, `provenance_violations` — how it currently sources the data-lane provenance baseline) and `canary.py`. Identify the minimal additive change so the provenance baseline (the registry to diff against) is an injected parameter with a default that exactly reproduces today's data-lane behavior.
- [ ] **Step 2 (failing test):** a test that calls `provenance_violations` with an injected engine baseline (`DISPOSITION_POLICIES`-shaped) and asserts engine-correct behavior; AND re-run the existing #187 fence/canary tests unchanged as the regression gate (they must still pass byte-identical). Run → FAIL (param not yet added).
- [ ] **Step 3:** Implement the additive parameter (keyword-only, default = current data-lane source). No behavior change when the arg is omitted. Add the engine hard-denied paths set as data (not hardcoded into the function) — the engine caller passes its denied set incl. `ops/engine_supervisor.py`, `ops/aar_autotune.py`, `tpcore/supervisor_state.py`, `ops/engine_ladder.py` mechanism + the shared protected paths.
- [ ] **Step 4:** Run engine test + the full #187 fence/canary suite → all PASS (data lane provably unaffected).
- [ ] **Step 5:** Commit.

### Task 1.2: `tpcore/engine_llm_triage/select.py` (TDD)
- [ ] **Step 1:** Read `ops/engine_ladder.list_undispositioned(pool)` (its return shape, the `hold_id` key, the open/grace/escalate-only semantics it already encodes) and `tpcore/llm_data_triage/select.py` (the #187 select shape + `MAX_TRIAGE_PER_CYCLE`).
- [ ] **Step 2 (failing test, fake pool):** `select_novel_escalations(pool) -> list[EngineNovelEscalation]` = `engine_ladder.list_undispositioned(pool)` → drop any `hold_id` with a prior `ENGINE_LLM_TRIAGE_PROPOSAL` → oldest-first, capped at `MAX_TRIAGE_PER_CYCLE`. Assert: it calls `list_undispositioned` (does NOT reimplement open-set/grace/escalate-only), dedups by prior proposal, caps, and does NOT test `policy_for() is None` (proven dead — §7). Run → FAIL.
- [ ] **Step 3:** Implement exactly that (thin; reuse `list_undispositioned`).
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit.

### Task 1.3: `tpcore/engine_llm_triage/packet.py` (TDD)
- [ ] **Step 1:** Read `tpcore/llm_data_triage/packet.py` (`build_packet`, deterministic `packet_hash`, size cap/truncation) and the engine context sources: the `ENGINE_ESCALATED` payload, `tpcore.supervisor_state.current_hold(pool, engine)`, open `platform.forensics_triggers` for the engine, the engine profile, and `engine_ladder.policy_for(failure_class)` (advisory recommended-default + rationale ONLY — never a selection gate).
- [ ] **Step 2 (failing test, fake pool):** `build_packet(pool, esc) -> EngineTriagePacket` read-only; deterministic `packet_hash` (same input → same hash); size cap + truncation marker; packet includes the escalation, `current_hold`, open forensics triggers, engine profile, and the advisory `policy_for` default+rationale. Run → FAIL.
- [ ] **Step 3:** Implement (mirror #187 packet shape; engine inputs).
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit.

### Task 1.4: `docs/engine_llm_triage_persona.md` + `PERSONA_VERSION` lockstep (TDD)
- [ ] **Step 1:** Read `docs/llm_data_triage_persona.md` + its lockstep test.
- [ ] **Step 2 (failing test):** `tpcore/engine_llm_triage/__init__.py::PERSONA_VERSION` lockstep with the persona doc header version; persona states the engine output contract (additive `DISPOSITION_POLICIES` binding proposing an EXISTING `EngineEscalationDisposition` verb; dossier; confidence; explicit "could not determine"), the hard guardrails (no authority, defer to R3 human, never invent internals, never propose a new mechanism/enum member), and "NOT a safety boundary". Run → FAIL.
- [ ] **Step 3:** Author the persona doc (engine-native, mirror #187 structure) + set `PERSONA_VERSION`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit. Then Phase-1 PR: full suite green (incl. #187 regression), ruff clean, dark (no caller), gated PR, CI-green, squash-merge, sync.

---

## Phase 2 — The agent + official Anthropic call (mocked, dark) (gated PR #3)

Branch `feat/engine-triage-p2`.

**Files:**
- Create: `ops/engine_llm_triage.py`
- Test: `tests/test_engine_llm_triage_agent.py` (host-repo guard per git-hygiene rule 3; importlib-load to dodge `scripts/ops.py`↔`ops/` shadowing — the data-lane precedent)

### Task 2.1: re-pin official Anthropic shape
- [ ] **Step 1:** Via context7 (`/anthropics/anthropic-sdk-python` + docs.claude.com) re-fetch & pin the current model id / SDK / `Message.content/stop_reason/usage` shape. Capture the doc ref in this plan + the spec §6. (`feedback_use_official_docs` — do not code from memory.)

### Task 2.2: engine `run_triage` reusing the shipped wrapper (TDD)
- [ ] **Step 1:** Read `ops/llm_data_triage.py` (`run_triage` flow, the Anthropic-call/no-key/`AuthenticationError`/malformed-per-escalation-isolation wrapper, `_emit`/`_INSERT_SQL`, crash-isolation, `_open_draft_pr` sandbox) and `ops/engine_ladder._emit`/its `_INSERT_SQL`.
- [ ] **Step 2 (failing tests, mocked client; no live calls):** mirror the #187 agent test set, engine-flavoured: calls SDK with pinned model, **no `tools`**, `temperature=0.0`, persona as `system`; emits non-authoritative `ENGINE_LLM_TRIAGE_PROPOSAL` (payload incl. `hold_id`, `failure_class`, `persona_version`, `packet_hash`, proposed disposition, model/usage) via the engine `_emit`/`_INSERT_SQL`; no-key ⇒ safe no-op (`skipped_no_key`, zero retries — `AuthenticationError` bypasses `with_retry`); RuntimeError ⇒ crash-isolated (`out.error`, no raise); empty-content / non-dict-JSON ⇒ per-escalation-isolated (skip one, batch continues); import-isolation AST test (the agent + its module must NOT import `tpcore.risk`/`order_management`/`engine_supervisor`/`aar_autotune`/`engine_ladder` *actor* paths — it MAY import `engine_ladder.list_undispositioned`/`policy_for` read predicates; the AST guard's forbidden set is the actor/mutation entrypoints — define precisely and assert it bites). Run → FAIL.
- [ ] **Step 3:** Implement `ops/engine_llm_triage.py`: reuse the shipped `ops.llm_data_triage` Anthropic-call/no-key/malformed wrapper *verbatim* (import + call; do NOT re-author the SDK envelope); engine `select`→`packet`→call→emit; `_open_draft_pr` reused with the engine additive-binding stub + dossier (still draft + human-merge-only, credential-starved env-allowlist, worktree/branch/tmpdir always cleaned, #63-hardened prune, never merges). Crash-isolated; dark (no daemon caller yet; no PR opened in CI — mocked).
- [ ] **Step 4:** Run → all PASS; full suite green; `git -C <repo> branch --list 'llm-triage/*'` empty (no test leak).
- [ ] **Step 5:** Commit. Phase-2 PR: gated, CI-green, squash-merge, sync.

---

## Phase 3 — Wire event-driven (FORK B = B1) + CI fence + digest (gated PR #4)

Branch `feat/engine-triage-p3`.

**Files:**
- Modify: `ops/llm_triage_service.py` (add a second `_run_supervised` co-task: engine loop, `ENGINE_ESCALATED` cursor-poll, shared advisory pool, crash-isolated from the data loop)
- Modify: `.github/workflows/ci.yml` (extend the label-gated `llm-triage-fence` job to also run the engine provenance/hard-denied check; never references `ANTHROPIC_API_KEY`)
- Modify: `scripts/llm_triage_pr_check.py` (engine registries baseline — reuse the #63-hardened worktree handling)
- Modify: `ops/weekly_digest.py` (surface the engine proposal on the engine escalation's digest line — reuse the existing builder; DRY)
- Test: extend `tests/test_llm_triage_service.py`; `scripts/tests/test_two_daemon_invariant.py` MUST pass UNEDITED; `scripts/tests/test_llm_triage_pr_check_cleanup.py`; weekly_digest test

### Task 3.1: engine co-task in `ops/llm_triage_service.py` (TDD)
- [ ] **Step 1:** Read `ops/llm_triage_service.py` (the data `_run_supervised` co-task, `_find_new_trigger` cursor-poll, `mkdir`-atomic lock, `_startup_worktree_prune`, `_main_loop`, shared pool, `main()`).
- [ ] **Step 2 (failing test):** the daemon now runs TWO independent `_run_supervised` co-tasks; the engine task triggers on `ENGINE_ESCALATED` (cursor-poll), calls `ops.engine_llm_triage.run_triage`, is crash-isolated from the data task (engine task raising does not kill the data task or the daemon), shares the one advisory pool. Assert `scripts/tests/test_two_daemon_invariant.py` passes with ZERO edits (installer/label/4-token whitelist unchanged). Run → FAIL.
- [ ] **Step 3:** Implement the engine co-task beside the data one (generalise the daemon docstring to "triage-service"; installer/label/whitelist UNCHANGED). Reuse all idioms verbatim.
- [ ] **Step 4:** Run → PASS; `test_two_daemon_invariant.py` green unedited (if it needs an edit, STOP — placement is wrong).
- [ ] **Step 5:** Commit.

### Task 3.2: CI fence (engine registries) + digest surfacing (TDD)
- [ ] **Step 1:** Read `.github/workflows/ci.yml` `llm-triage-fence` job + `scripts/llm_triage_pr_check.py` (the #63-hardened base-loader) + `ops/weekly_digest.py` engine-escalation line builder.
- [ ] **Step 2 (failing tests):** the fence check also evaluates the engine provenance baseline (`DISPOSITION_POLICIES`) + engine hard-denied paths for an `engine-llm-triage`-labelled PR; ci.yml never references `ANTHROPIC_API_KEY`; the pr-check base loader keeps the #63 prune-fallback (extend `test_llm_triage_pr_check_cleanup.py`); the engine proposal is appended to the engine escalation's weekly-digest line (reuse the builder; no re-query). Run → FAIL.
- [ ] **Step 3:** Implement (extend the existing job/script/digest builder — additive, label-gated; credential-starved).
- [ ] **Step 4:** Run → PASS; YAML valid; `! grep ANTHROPIC ci.yml`; full suite green; no host-repo git leak.
- [ ] **Step 5:** Commit. Phase-3 PR: gated, CI-green (wait for run to register then watch), squash-merge, sync.

---

## Phase 4 — Docs (gated PR #5)

Branch `docs/engine-triage-p4`.

**Files:** `docs/ENGINE_ESCALATION_HARDENING_LADDER.md` (R5 OUT→BUILT, same R1–R4 convention), `CLAUDE.md` (engine-lane bullet), `docs/llm_data_triage_operator_runbook.md` (extend the SHARED runbook for both lanes — engine label, the engine daemon co-task), the spec (`Status:` → BUILT + Build record P0/P1/P2/P3/P4 PR numbers), memory `project_engine_llm_triage_ownership` → BUILT.

- [ ] **Step 1:** Reconcile each doc to shipped reality (accuracy-review discipline: no overclaim; cross-check claims vs merged code; build-record PR numbers verified merged).
- [ ] **Step 2:** `git diff --stat` = docs only; collection clean; gated PR; CI-green; squash-merge; sync.
- [ ] **Step 3:** Update memory `project_engine_llm_triage_ownership.md` → BUILT (data-lane session retains ownership record); update `MEMORY.md` line.

---

## Self-Review

**1. Spec coverage:** §1 parity → P1–P4 + the §1/§7a parity framing; §2 engine-stays-deterministic → agent never mutates, draft-only (P2); §3 fence reuse + engine registries/hard-denied → Task 1.1 + 2.x + 3.2; §4 vetoes → provenance+hard-denied (1.1/3.2); §5 persona → 1.4; §6 official SDK reuse → 2.1/2.2; §7 FIXED novelty predicate → 1.2 (`list_undispositioned`, not `policy_for() is None`); §7a deterministic detection-gap → **Phase 0** (0.1–0.5); §8 B1 placement → 3.1 (topology test unedited); §9 scope/LLM-never-detector → Phase 0 deterministic + §9; §10 phasing → Phases 0–4; §11 all resolved → carried. ✓

**2. Placeholder scan:** the only deliberate deferral is Task 0.1 (a plan-time expert sub-pass to *freeze* the silent-absence detection scope) — that is an explicit, owned decision step, not a TBD; its output is recorded into the spec/task list before any 0.2+ code. Every other task has exact files + failing-test + impl + verify + commit. No "TODO/handle edge cases" language.

**3. Type consistency:** `select_novel_escalations(pool)->list[EngineNovelEscalation]`; `build_packet(pool,esc)->EngineTriagePacket` + `packet_hash`; `PERSONA_VERSION`; `ENGINE_LLM_TRIAGE_PROPOSAL` payload keys consistent P2↔P3; the injected provenance-baseline param name consistent Task 1.1↔2.2↔3.2; `run_triage` signature mirrors the shipped `ops.llm_data_triage` reuse. Phase-0 `failure_class` name(s) frozen in Task 0.1 and used identically in 0.2/0.3/0.4.

Execution: subagent-driven-development — fresh implementer per task, split spec-then-code-quality reviews, gated PR per phase, CI-green before merge, branch-hygiene before every commit.
