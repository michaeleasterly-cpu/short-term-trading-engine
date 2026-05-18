# Consolidated Defect Register (#254) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]` checkboxes.

**Goal:** One consolidated, queryable defect view that unifies escalation-class defects (both Ladders) + the today-homeless review-found-defect class, **without** a new authoritative table/SoT.

**Architecture:** Spec `docs/superpowers/specs/2026-05-19-consolidated-defect-register-design.md` (v1, operator-approved). A **derived read-model** (`ops/defect_register.py`) that calls the existing Ladder SoTs *verbatim* + **one** minimal new `application_log` event class (`REVIEW_DEFECT_LOGGED`/`REVIEW_DEFECT_RESOLVED`) for the review-found class. No new table, no migration, no daemon, no new write-coupling on existing producers. Forcing-function CI tests enforce parity (register ≡ Ladders) and TODO-coverage. **Build held behind #253** (SP3-T9 guard blocks the merge queue) — implement on a parked branch; push/PR/merge only once main is unblocked.

**Tech Stack:** Python 3.11, asyncpg, structlog, pydantic v2, pytest (`asyncio_mode=auto`), ruff. Gated PR per phase; CI-green before merge; branch-hygiene (`git switch -c`, verify branch before every commit; tests never touch real DB/`data/` — fake pool).

**Reference (read, do not re-derive):** `ops/engine_ladder.py` (`list_undispositioned`, `_CANDIDATE_SQL`, `_DISPOSITIONED_EVENT`, `_emit`, the CLI shape, `escalation_drift` as the forcing-test pattern), `ops/weekly_digest.py` (`build_weekly_digest().undispositioned`, its `_emit`, the disposition CLI verb), `platform/migrations/.../*application_log*` (schemaless `data jsonb`; the `DBLogHandler` 7-day retention + any existing retention-exemption mechanism), `dashboard.py` (`_fetch_escalation_state`/`_fetch_escalation_state_cached` pattern ~2118-2197) + `dashboard_components/escalation.py`, `TODO.md` (the reconciled `[lane/gate/decision/effort]` defect/follow-up line convention), `docs/superpowers/specs/2026-05-18-dashboard-escalation-audit-panel-design.md` (the render-the-SoT doctrine).

---

## Phase DR1 — derived read-model + `list` CLI + parity forcing-test (gated PR #1; DARK)

Branch `feat/defect-register-dr1` off fresh `main` (when #253 unblocks; build now on the branch, hold push).

**Files:** Create `ops/defect_register.py`, `tests/test_defect_register.py`.

### Task DR1.1: the unified read-model (TDD)
- [ ] **Step 1:** Read `engine_ladder.list_undispositioned` (return shape, `hold_id`, the `_CANDIDATE_SQL` open-predicate) and `weekly_digest.build_weekly_digest` (the `.undispositioned` field shape + how it's annotated with the data-lane Ladder policy/reason).
- [ ] **Step 2 (failing test, fake pool):** `async def consolidated_defects(pool) -> list[DefectRow]` where `DefectRow` (pydantic/dataclass, typed) = `{defect_ref, origin (escalation|review|todo), engine|lane, summary, state, opened_at, policy/disposition, fix_ref|None}`. It MUST: call `engine_ladder.list_undispositioned(pool)` and `weekly_digest.build_weekly_digest(pool).undispositioned` **verbatim** (no re-query of `application_log` for escalation state); map each to a `DefectRow` with `defect_ref = hold_id` and `origin="escalation"`; lifecycle `state` derived (open / dispositioned-structural-parked) from the existing disposition events the Ladders already expose — NOT re-derived. Tests: an engine undispositioned escalation appears once; a data-lane one appears once; a row that is in BOTH Ladders' sets (shouldn't, but) collapses by `defect_ref` (join, never sum); empty → empty; the function does not itself issue an `application_log` escalation query (assert via the fake pool that only the Ladder APIs are invoked — spy/guard).
- [ ] **Step 3:** Implement as a thin composer (import + call both Ladder APIs; map; `defect_ref` join). No new SQL for escalation state.
- [ ] **Step 4:** Run → PASS. Commit.

### Task DR1.2: `list` CLI + the parity forcing-test (TDD)
- [ ] **Step 1:** Read the `engine_ladder` / `weekly_digest` `__main__`/CLI idiom (argparse, explicit non-zero exit, the `ops`-not-a-package importlib precedent if needed for tests).
- [ ] **Step 2 (failing tests):** `python -m ops.defect_register list` prints the consolidated rows (stable, grep-able; deterministic order by `opened_at, defect_ref`). **Parity forcing-test** (mirror `escalation_drift()` test style): the register's escalation-origin `defect_ref` set ≡ (`engine_ladder.list_undispositioned` ∪ data-lane `undispositioned`) as a SET — fails the build if the register drifts from / re-derives the Ladders. Run → FAIL.
- [ ] **Step 3:** Implement the CLI shim (`# pragma: no cover` on the `main()` entry per precedent) + the parity test.
- [ ] **Step 4:** Run → PASS; full suite (excluding the known #253 blocker) 0 failed; ruff clean. Commit. (Phase DR1 PR is prepared but **held** behind #253.)

---

## Phase DR2 — the missing primitive: review-found defect events + retention-exemption + TODO forcing-test (gated PR #2)

Branch `feat/defect-register-dr2` off DR1-merged `main`.

**Files:** Modify `ops/defect_register.py` (the thin emit helper + consume the new events), the retention-exemption point (identify it — see DR2.1), `tests/test_defect_register.py` (+ a new TODO-parity test file if cleaner).

### Task DR2.1: confirm/extend the application_log retention-exemption (read-then-TDD)
- [ ] **Step 1:** Read the `DBLogHandler`/`application_log` retention prune (the 7-day delete — find it: `grep -rn "application_log" platform/migrations scripts ops tpcore | grep -i "delete\|retention\|interval '7"`). Determine the EXISTING exemption mechanism (an `event_type` allowlist? a `data` flag? a severity?). If a clean exemption precedent exists → reuse it for `REVIEW_DEFECT_LOGGED`/`_RESOLVED`. If NONE exists → the minimal exemption is the smallest change to the prune predicate to never delete these two event types (state which; keep it surgical, no schema change).
- [ ] **Step 2 (failing test):** a test asserting a `REVIEW_DEFECT_LOGGED` row OLDER than the retention window is NOT pruned (drive the actual prune logic against a fake pool with an aged row); a normal log row of the same age IS pruned (control). Run → FAIL.
- [ ] **Step 3:** Implement the exemption (reuse precedent or the minimal prune-predicate change).
- [ ] **Step 4:** Run → PASS. Commit.

### Task DR2.2: `REVIEW_DEFECT_LOGGED`/`_RESOLVED` emit + `log`/`resolve` CLI + register integration (TDD)
- [ ] **Step 1:** Read `engine_ladder._emit` (the exact `application_log` INSERT shape) — mirror byte-for-byte.
- [ ] **Step 2 (failing tests):** `python -m ops.defect_register log --ref <#NNN|slug> --summary "..." [--lane ...]` emits one `REVIEW_DEFECT_LOGGED` (payload `{schema, defect_ref, origin:"review", summary, lane, pr:None, logged_at}`); `resolve --ref <r> --pr <#NNN|sha>` emits `REVIEW_DEFECT_RESOLVED`; `consolidated_defects` now ALSO surfaces open review defects (open = a `LOGGED` with no later matching `RESOLVED` — the SAME anti-join pattern as `_DISPOSITIONED_EVENT`); a resolved one shows `state=fixed` + `fix_ref`; dedup: a review defect whose `defect_ref` also equals an escalation `hold_id` collapses to ONE row (join). Tests deterministic, fake pool, no real DB.
- [ ] **Step 3:** Implement the thin emit helper (mirror `_emit`) + CLI + register consumption (anti-join open-predicate). No new write-coupling anywhere else.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5 — TODO-parity forcing CI test:** a test asserting every `TODO.md` line tagged a still-open defect (define the exact tag convention read from the reconciled TODO — e.g. lines containing `[lane:` AND an open-marker) has a matching open `REVIEW_DEFECT_LOGGED` (by `defect_ref`/anchor) — so a review-found defect cannot live only in TODO.md. It must genuinely bite (a TODO defect line with no event ⇒ red). Run → PASS (seed the convention so existing TODO defect lines are represented, or scope the test to lines added under the new convention — state the choice; do NOT retro-fail historical TODO lines spuriously). Commit. (PR held behind #253.)

---

## Phase DR3 — render-only dashboard panel + docs reconcile (gated PR #3)

Branch `feat/defect-register-dr3` off DR2-merged `main`.

**Files:** Modify `dashboard.py` (+ `dashboard_components/` if a pure classifier helper fits the established pattern), `TODO.md`, `CLAUDE.md` (ops line), the spec (→ BUILT + build record), memory.

- [ ] **Step 1:** Read `dashboard.py` `_fetch_escalation_state`/`_fetch_escalation_state_cached`/`render_*` + `dashboard_components/escalation.py` (the pure `classify_*` → `(color,summary,detail)` tuple pattern). Add a render-only "Defect Register" panel that calls `ops.defect_register.consolidated_defects` (reuse the fetch+cache idiom byte-for-byte) and renders via the existing `_render_health_row`/expander pattern. **Recompute nothing; no write button** (panel-spec non-goal). Pure-classifier unit tests (fabricated rows, no DB/Streamlit) per the `escalation.py` test precedent; import-smoke for `dashboard.py`.
- [ ] **Step 2:** Docs reconcile (accuracy discipline, no overclaim): `TODO.md` (the new review-defect convention + this register as the surface), CLAUDE.md one ops line, this spec → `Status: BUILT` + Build record (DR1/DR2/DR3 PRs), memory note. `git diff --stat` = the panel + docs only.
- [ ] **Step 3:** Gated PR; CI-green; squash-merge; sync.

---

## Self-Review

**1. Spec coverage:** §1 derived-read-model-not-new-table → DR1 (composes the Ladder APIs verbatim, no new table); §2 taxonomy/`defect_ref` join/lifecycle → DR1.1 + DR2.2; the missing review-found primitive on existing `application_log` → DR2.2; §3 never-mask invariants → the DR1.2 parity forcing-test + DR2.2 anti-join open-predicate + the "no escalation re-query" spy-guard; the retention risk (§5) → DR2.1 (confirm/extend exemption + an aged-row test); §4 lives in `ops/defect_register.py`, render-only panel, both Ladders consumed not reimplemented (symmetry-not-copy) → DR1/DR3; §5 OUT (no table/daemon/migration/write-button) → enforced (no migration task exists; panel is render-only); forcing-function (TODO-parity) → DR2.5. ✓

**2. Placeholder scan:** the "find it by reading" items (the retention-prune location + existing exemption mechanism; the exact TODO open-defect tag convention) are explicit read-then-decide steps grounded in named files with a stated fallback — not TBDs. Every task has files + failing test + impl + verify + commit. The TODO-parity test's "don't spuriously retro-fail historical lines" is an explicit, owned scoping decision.

**3. Type/name consistency:** `consolidated_defects(pool) -> list[DefectRow]`; `DefectRow` fields (`defect_ref, origin, lane, summary, state, opened_at, fix_ref`) consistent DR1↔DR2↔DR3; event names `REVIEW_DEFECT_LOGGED`/`REVIEW_DEFECT_RESOLVED` consistent DR2↔register↔retention-exemption↔TODO-parity; `defect_ref` join key consistent throughout; CLI verbs `list|log|resolve` consistent.

Execution: subagent-driven-development — fresh implementer per phase, split spec-then-code-quality reviews (adversarial on never-mask/no-re-derivation + the retention exemption), gated PR per phase, CI-green before merge, branch-hygiene before every commit. **All builds held behind #253**; implement+review on parked branches, push/PR/merge on unblock.
