# Lean Dev Env + Codebase Health Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]` checkboxes.

**Goal:** Cut the dev/CI test loop ~4–7×, make the 1.4 GB `data/` invisible to tool walks, and add standing dead-code / orphan-script / duplication / best-practice CI enforcement layered on the already-solved import guard — all additively, preserving every live-money and cross-session invariant.

**Architecture:** Spec `docs/superpowers/specs/2026-05-19-lean-dev-env-codebase-health-design.md` (v1, operator-approved). Phases P1–P4 are additive (config + new CI checks + new test files in existing testpaths) → cross-session-safe, built now. P5 (de-dup/tpcore reconsolidation) is code-mutating/high-collision → a SEPARATE plan when the engine session ends (with #189/#252) — NOT in this plan. One gated PR per phase (P3 splits into sub-PRs). Standard subagent-driven discipline: fresh implementer → split spec/intent then code-quality review → gated PR → CI authoritative via `gh pr checks` → whole-suite single-process gate → squash-merge → sync.

**Tech Stack:** Python 3.11, pytest (`asyncio_mode=auto`), pytest-xdist, ruff, vulture, asyncpg, structlog. Branch-hygiene: `git switch -c`, verify branch before every commit; no `git stash`; tests never touch real DB/git/`data/`; never the engine session's files/worktrees/stash.

**Reference (read, do not re-derive):** the spec; `pyproject.toml` (`[project.optional-dependencies].dev`, `[tool.ruff]` ~`select`, `[tool.pytest.ini_options]` ~`asyncio_mode`/`testpaths` incl. the generated engine-manifest fenced region); `.github/workflows/ci.yml` (the install step, the `pytest`/`ruff`/`check_imports` job, the two `*-triage` fence jobs + the new `agent-pr-label-guard` job — do NOT edit existing jobs); `tpcore/scripts/check_imports.py` (the already-solved import-layering guard — do NOT duplicate); `tpcore/logging/db_handler.py` (`_RETENTION_SQL`, `RETENTION_EXEMPT_EVENT_TYPES`, the prune path) + `tpcore/tests/test_db_log_handler.py`; `ops/defect_register.py` `_REVIEW_OPEN_SQL` (the anti-join open-predicate P4 must not break); the `sys.modules['ops']` snapshot/restore precedent in `tests/test_llm_triage_service.py` / `tests/test_defect_register.py`; `[[feedback_ops_package_shadow_full_suite_gate]]`.

**Standing acceptance gate for EVERY task (the ops/ package-shadow + streamlit hazards):** the authoritative gate is the WHOLE single-process suite `python -m pytest -q` (0 failed, count ≥ the current main baseline — re-measure baseline at task start, it grows as the engine lane merges) + order-flip `python -m pytest -q tpcore/tests/test_ops.py tpcore/tests/test_ops_helpers.py tests/test_defect_register.py` AND reversed, both green. Subset-green ≠ CI-green. CI verified via `gh pr checks`, never `gh run watch`'s exit code.

---

## Phase P1 — Fast parallel test loop (gated PR #1)

Branch `feat/lean-p1-pytest-xdist` off fresh `main`.

**Files:** `pyproject.toml`, `.github/workflows/ci.yml`, the ~N `sys.modules['ops']`-poisoning test modules, new `tests/test_xdist_group_manifest.py`.

### Task P1.1: enumerate the ops-shadow test modules (read-then-record)
- [ ] **Step 1:** Run exactly: `grep -rlE "spec_from_file_location\\(\\s*[\"']?(ops|_ops|ops_under_test|_lts_under_test|_dr_under_test|_wd_under_test|wd)|sys\\.modules\\[[\"']ops[\"']\\]|sys\\.modules\\.get\\([\"']ops" tests/ tpcore/tests/ scripts/tests/ 2>/dev/null | sort -u` and ALSO `grep -rlE "scripts/ops\\.py|importlib.*\\bops\\b" tests/ tpcore/tests/ scripts/tests/ 2>/dev/null | sort -u`. Union the two lists. This is the authoritative `OPS_SHADOW_MODULES` set.
- [ ] **Step 2:** Record the exact relative paths in the PR description and as the `_EXPECTED` constant in the manifest test (Task P1.4). Expected to include at least `tests/test_llm_triage_service.py`, `tests/test_defect_register.py`, `tests/test_lab_isolation.py`, `tpcore/tests/test_handle_daily_bars_multi.py`, `tpcore/tests/test_ops.py`, `tpcore/tests/test_ops_helpers.py` — but the grep is authoritative; do not hardcode from memory.

### Task P1.2: add pytest-xdist dependency
- [ ] **Step 1:** In `pyproject.toml` `[project.optional-dependencies].dev`, add `"pytest-xdist>=3.6,<4"` (pin: current major; verify the installed/available version with `python -m pip index versions pytest-xdist` or context7 pytest-xdist docs — use the latest stable < 4).
- [ ] **Step 2:** In `.github/workflows/ci.yml`, the dev install (`pip install -e .[dev]`) already pulls extras — confirm no separate install line is needed; if the workflow pins a lockfile, regenerate per the repo's convention.
- [ ] **Step 3:** `pip install -e .[dev]` locally; `python -c "import xdist; print(xdist.__version__)"` succeeds. Commit.

### Task P1.3: mark the ops-shadow modules into one xdist group
- [ ] **Step 1:** To EACH module in the P1.1 set, add at module top (after imports, before tests): `import pytest` (if absent) and `pytestmark = pytest.mark.xdist_group("ops_shadow")`. If a module already defines `pytestmark`, append into a list: `pytestmark = [<existing>, pytest.mark.xdist_group("ops_shadow")]`. Do NOT alter the existing snapshot/restore `sys.modules['ops']` logic in those files.
- [ ] **Step 2:** Register the marker to silence unknown-marker warnings: in `pyproject.toml` `[tool.pytest.ini_options]` add/extend `markers = ["xdist_group: pytest-xdist group pinning (ops-shadow single-worker isolation)"]` (only if a `markers` key convention is used; xdist registers it dynamically, but register for `-W error` safety — check existing config first).
- [ ] **Step 3:** Run `python -m pytest -n auto --dist loadgroup -q 2>&1 | tail -5` → 0 failed, count == serial baseline. Then the **authoritative serial gate** `python -m pytest -q 2>&1 | tail -5` → identical pass count, 0 failed. Then both order-flips (serial). All green. Commit.

### Task P1.4: the group-membership sentinel test (TDD)
- [ ] **Step 1 (failing test):** Create `tests/test_xdist_group_manifest.py`:
```python
"""Clockwork guard: every test module that loads scripts/ops.py or
mutates sys.modules['ops'] MUST carry the ``xdist_group("ops_shadow")``
mark, else parallel runs go flaky (the ops/ package-shadow is a
single-process invariant; loadgroup keeps the group on one worker).
Mirrors the gen_engine_manifest manifest-discipline."""
from __future__ import annotations
import ast, re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_TEST_DIRS = ["tests", "tpcore/tests", "scripts/tests"]
_SHADOW_RE = re.compile(
    r"sys\.modules\[[\"']ops[\"']\]|sys\.modules\.get\([\"']ops"
    r"|spec_from_file_location\([^)]*ops|scripts/ops\.py")

def _has_group_mark(src: str) -> bool:
    return 'xdist_group("ops_shadow")' in src or "xdist_group('ops_shadow')" in src

def test_every_ops_shadow_module_is_grouped() -> None:
    missing = []
    for d in _TEST_DIRS:
        for p in (_REPO / d).rglob("test_*.py"):
            src = p.read_text()
            if p.name == "test_xdist_group_manifest.py":
                continue
            if _SHADOW_RE.search(src) and not _has_group_mark(src):
                missing.append(str(p.relative_to(_REPO)))
    assert not missing, (
        "ops-shadow test modules missing xdist_group('ops_shadow') "
        f"(parallel-flaky): {missing}")
```
- [ ] **Step 2:** Run `python -m pytest tests/test_xdist_group_manifest.py -q` → PASS (P1.3 already marked them). Temporarily remove one mark, re-run → it must FAIL listing that file (prove it bites), then restore the mark.
- [ ] **Step 3:** Add `tests/test_xdist_group_manifest.py` to the `OPS_SHADOW_MODULES`? NO — it does not load ops.py; leave unmarked (the `continue` self-exempt is correct). Commit.

### Task P1.5: CI — parallel accelerator + retained authoritative serial gate
- [ ] **Step 1:** Read the existing `pytest` step in `.github/workflows/ci.yml`. Replace ONLY the test invocation so CI runs TWO steps in the existing job (do not touch the fence jobs / `agent-pr-label-guard`):
  - Step "pytest (parallel accelerator)": `python -m pytest -n auto --dist loadgroup -q`
  - Step "pytest serial + order-flip (AUTHORITATIVE gate)": `python -m pytest -p no:xdist -q` then `python -m pytest -p no:xdist -q tpcore/tests/test_ops.py tpcore/tests/test_ops_helpers.py tests/test_defect_register.py` then the reversed module order. All must pass; this step is the gate of record.
  Keep `ruff` + `check_imports` steps unchanged.
- [ ] **Step 2:** Validate YAML parses: `python -c "import yaml,sys; print(list(yaml.safe_load(open('.github/workflows/ci.yml'))['jobs']))"` — all jobs present, fences + guard untouched.
- [ ] **Step 3:** Full local gate (serial whole-suite + both order-flips + the parallel run) all green; `ruff check` clean. Commit. **Land via gated PR** (split spec/intent then code-quality review; CI authoritative via `gh pr checks`; the new parallel step must be green AND the serial+order-flip step green; squash-merge; sync).

---

## Phase P2 — Hide the 1.4 GB tree (gated PR #2)

Branch `feat/lean-p2-toolwalk-excludes` off P1-merged `main`.

**Files:** `pyproject.toml`, new repo-root `.ignore`.

### Task P2.1: ruff + pytest excludes (TDD-ish via behavior check)
- [ ] **Step 1:** In `pyproject.toml` `[tool.ruff]` add `extend-exclude = ["data", "logs", "*_archive", ".claude"]` (merge if `extend-exclude`/`exclude` already present — do not drop existing entries; verify against current ruff docs that `extend-exclude` is the right key for the installed ruff version).
- [ ] **Step 2:** In `[tool.pytest.ini_options]` add `norecursedirs = ["data", "logs", ".claude", "*_archive", ".git", ".venv"]` (merge with any existing).
- [ ] **Step 3 (verification, not a unit test):** Confirm no test fixture lives under an excluded path: `grep -rlE "data/|_archive/" tests/ tpcore/tests/ scripts/tests/ | xargs grep -lE "open\(|Path\(.*data|read_text|tmp_path" 2>/dev/null | head` — manually confirm any hit uses `tmp_path`/`tests/`-local data, NOT a real `data/` recurse. Run the whole suite: collection count unchanged (no tests lost to `norecursedirs`), 0 failed.

### Task P2.2: tracked `.ignore` for ripgrep/fd/editors
- [ ] **Step 1:** Create repo-root `.ignore` (NOT `.gitignore`; tracked) with exactly:
```
data/
logs/
*_archive/
.claude/worktrees/
.venv/
```
- [ ] **Step 2:** Verify: `rg --files -g '!.git' | grep -c '^data/'` → 0 (rg honors `.ignore`); a normal `rg tpcore` is fast and returns no `data/` hits.
- [ ] **Step 3:** Full gate (whole suite + order-flips), ruff clean. Commit. Land via gated PR (split review; CI authoritative; merge; sync).

---

## Phase P3 — Standing codebase-health enforcement (gated sub-PRs #3a–#3e)

Each sub-task = its own branch off the prior-merged `main` + its own gated PR (keeps reviews focused and lets the engine session rebase cleanly).

### P3a — `vulture` dead-code gate (branch `feat/lean-p3a-vulture`)
- [ ] **Step 1:** Add `"vulture>=2.11,<3"` to `[project.optional-dependencies].dev` (verify latest <3 via context7/pip). `pip install -e .[dev]`.
- [ ] **Step 2:** Generate the baseline allowlist READ-ONLY: `vulture tpcore ops reversion vector momentum sentinel canary dashboard_components --min-confidence 80 --make-whitelist > vulture_allowlist.py` (commit this file AS the baseline — it encodes today's intentional dark/feature-flagged code, e.g. canary non-graduation, LLM-triage fenced paths). Spot-read it to confirm it isn't masking an obvious real bug (sanity, not exhaustive).
- [ ] **Step 3 (failing test → gate):** Add a `scripts/tests/test_no_new_dead_code.py` that shells `vulture <pkgs> --min-confidence 80 vulture_allowlist.py` (via the repo's subprocess test idiom; NEVER against the live repo's git) and asserts exit 0; OR add a `.github/workflows/ci.yml` step "vulture (fail on new dead code)" running the same command (prefer the CI step + a thin test that the allowlist file exists and parses). It must FAIL if new un-allowlisted dead code is introduced (prove by adding a throwaway unused function in a scratch test fixture path, observing red, removing it).
- [ ] **Step 4:** Whole-suite gate green; ruff clean; YAML valid. Commit. Gated PR (split review — code-quality reviewer must confirm the allowlist isn't over-broad / masking real findings); merge; sync.

### P3b — orphan one-off script detector (branch `feat/lean-p3b-orphan-scripts`)
- [ ] **Step 1 (failing test):** Create `scripts/tests/test_no_orphan_scripts.py`: for each `scripts/*.py` (exclude `scripts/tests/`), assert it is referenced by at least one of: an `ops.py` stage registry entry, a `scripts/*.sh` wrapper, a daemon/launchd plist, `.github/workflows/ci.yml`, `pyproject.toml`, or another test/module (grep the repo excluding `data/`/`.git`). An `_ALLOWLIST` frozenset names deliberate standalone tools (e.g. `git_hygiene` peers, `agent_pr_label_guard.py`, `gen_engine_manifest.py`). Unreferenced ∧ not allowlisted ⇒ fail listing the orphan.
- [ ] **Step 2:** Run → it will flag `scripts/ingest_tradier_csv.py` (0 refs, per the sweep). DECIDE per CLAUDE.md (one-off scripts banned → should be an `ops.py` stage): if it is genuinely dead, this plan does NOT delete it (deletion = code-mutation/Phase-5-class) — instead add it to `_ALLOWLIST` with a `# TODO(P5): migrate to ops.py stage or remove — flagged 2026-05-19` comment so the gate is green now and the cleanup is tracked for the single-session window. State this decision in the PR.
- [ ] **Step 3:** Prove it bites: add a scratch `scripts/_zzz_orphan.py`, observe red, remove it. Whole-suite gate green. Commit. Gated PR (split review); merge; sync.

### P3c — read-only tpcore-duplication audit (branch `feat/lean-p3c-dup-audit`)
- [ ] **Step 1:** Add `scripts/audit_code_duplication.py` (a REPORT-ONLY tool, registered intent: it is an analysis command, allowlisted in P3b): runs `pylint --disable=all --enable=duplicate-code --ignore-paths='.*/(tests|data)/.*' reversion vector momentum sentinel canary tpcore 2>&1` (or an AST-hash near-dup scan if pylint unavailable — check deps; pylint is report-only here, do NOT add it as a gate) and writes findings to `docs/audits/2026-05-19-tpcore-duplication-audit.md`. It MUST NOT modify any source.
- [ ] **Step 2:** Run it once; commit the generated `docs/audits/2026-05-19-tpcore-duplication-audit.md` (the findings — input to the deferred Phase 5). Add a one-line pointer in the spec's Phase 5 section? NO (spec is merged/immutable; reference it from the audit doc instead).
- [ ] **Step 3:** A thin test asserts the audit script runs and emits the markdown (no real source mutation; subprocess against a `tmp_path` copy or a `--dry-run`/`--check` mode — never the live tree mutated). Whole-suite gate green. Commit. Gated PR (spec/intent review confirms it is genuinely read-only / cross-session-safe); merge; sync.

### P3d — staged ruff `DTZ` (branch `feat/lean-p3d-ruff-dtz`)
- [ ] **Step 1:** In `pyproject.toml` `[tool.ruff.lint]` (or `[tool.ruff]` per the installed schema) add `"DTZ"` to `select`. Run `ruff check --select DTZ .` → record violation count.
- [ ] **Step 2:** Baseline existing code so it does not explode: `ruff check --select DTZ --add-noqa .` (adds `# noqa: DTZ...` to existing violations only). Spot-check 3–5 of the added noqas are genuinely pre-existing (not a real UTC bug introduced) — if any is a real naive-datetime bug on a live path, STOP and report it (do not noqa a real bug; that's a finding, not a baseline).
- [ ] **Step 3:** Confirm `ruff check .` clean (DTZ now enforced for NEW code; existing baselined). Whole-suite gate green. Commit. Gated PR (code-quality reviewer spot-audits the `--add-noqa` diff for a masked real bug); merge; sync.

### P3e — staged ruff `SLF` (branch `feat/lean-p3e-ruff-slf`)
- [ ] **Step 1–3:** Identical procedure to P3d but `"SLF"` (private-member access — mechanizes the prose-only CLAUDE.md "never access `._store`/`._pool`" mandate). `--add-noqa` baseline; spot-check the diff for a real private-attr violation on a tpcore class (if found, that's a finding → report, do not silently noqa). `ruff check .` clean; whole-suite gate; gated PR (reviewer audits the baseline diff); merge; sync. (`SIM`/`RUF`/`PTH` are explicitly LATER/out of this plan.)

---

## Phase P4 — Bounded `REVIEW_DEFECT_*` retention (gated PR #4)

Branch `feat/lean-p4-review-defect-retention` off prior-merged `main`.

**Files:** `tpcore/logging/db_handler.py`, `tpcore/tests/test_db_log_handler.py`.

### Task P4.1: bounded secondary cap (TDD)
- [ ] **Step 1:** Read `tpcore/logging/db_handler.py` `_RETENTION_SQL` / `_RETENTION_EXEMPT_CLAUSE` / `RETENTION_EXEMPT_EVENT_TYPES` and the prune execution path; read `ops/defect_register.py` `_REVIEW_OPEN_SQL` to learn the EXACT open-predicate (a `REVIEW_DEFECT_LOGGED` with no later matching `REVIEW_DEFECT_RESOLVED`) so the new cap can NEVER prune an open defect.
- [ ] **Step 2 (failing test):** In `tpcore/tests/test_db_log_handler.py` add tests against the fake-pool prune path: (a) a `REVIEW_DEFECT_LOGGED` row younger than 180 d is NOT pruned; (b) a `REVIEW_DEFECT_*` row older than 180 d but among the most-recent-2000 is NOT pruned; (c) a `REVIEW_DEFECT_*` row older than 180 d AND beyond the most-recent-2000 IS pruned — UNLESS (d) it is an OPEN defect (a `LOGGED` with no later `RESOLVED`), which is NEVER pruned regardless of age/count; (e) ordinary non-exempt rows still pruned at 7 d (control, unchanged). Run → FAIL.
- [ ] **Step 3:** Implement: extend the prune SQL/path with a SECONDARY bounded delete for `REVIEW_DEFECT_*` only — delete a `REVIEW_DEFECT_*` row iff `recorded_at < now()-interval '180 days'` AND it is NOT within the most-recent-2000 `REVIEW_DEFECT_*` rows AND it is NOT part of an open (unresolved) defect (anti-join mirroring `_REVIEW_OPEN_SQL` / the DR2 retention-exemption invariant). Single source: reuse `RETENTION_EXEMPT_EVENT_TYPES`; build the secondary clause once at module load like the existing `_RETENTION_EXEMPT_CLAUSE` (constant-derived, no injection, empty-set safe). Do NOT introduce a new prune mechanism/daemon — same `DBLogHandler` path.
- [ ] **Step 4:** Run → PASS. Whole-suite gate (the 180 d/2000 numbers as module constants `_REVIEW_DEFECT_MAX_AGE_DAYS = 180`, `_REVIEW_DEFECT_MAX_ROWS = 2000`). ruff clean. Commit. Gated PR (spec/intent review adversarial on "an open defect can never be pruned" + the anti-join correctness; code-quality review); merge; sync.

---

## Phase P5 — Actual de-dup / tpcore reconsolidation

**NOT in this plan.** Code-mutating, high cross-session collision. Its input is `docs/audits/2026-05-19-tpcore-duplication-audit.md` (P3c). It gets its own brainstorm/plan in the single-session window when the operator ends the engine session (with #189/#252). Do not start it under this plan.

---

## Self-Review

**1. Spec coverage:** spec §3 P1 fast loop → Phase P1 (xdist+loadgroup+group mark+sentinel+retained authoritative serial/order-flip gate); §3 P2 hide tree → Phase P2 (ruff/pytest excludes + tracked `.ignore`, `data/` not moved); §3 P3(a) vulture → P3a; §3 P3(b) orphan scripts → P3b; §3 P3(c) read-only dup audit (not import-linter) → P3c; §3 P3(d) staged ruff DTZ→SLF → P3d/P3e; §3 P4 bounded REVIEW_DEFECT_* (D4) → P4; §3 P5 deferred → explicitly excluded with its trigger. Invariants (§2): every task carries the whole-suite single-process + order-flip authoritative gate; parallel = accelerator only; allowlist-baselined gates; additive/no-stomp; live-path untouched. ✓

**2. Placeholder scan:** the "enumerate by grep" (P1.1), "verify latest version" (P1.2/P3a), "spot-check the --add-noqa diff for a real bug" (P3d/e) are explicit read/verify-then-record steps with exact commands and a stated fail-action (a real bug ⇒ report, never silently noqa) — not TBDs. P3b's `ingest_tradier_csv.py` decision is owned (allowlist + tracked TODO, no Phase-5-class deletion). Every task has files + command/test + verify + commit + gated-PR. ✓

**3. Type/name consistency:** `xdist_group("ops_shadow")` string, `OPS_SHADOW_MODULES` set, `vulture_allowlist.py`, `_ALLOWLIST`, `_REVIEW_DEFECT_MAX_AGE_DAYS=180`/`_REVIEW_DEFECT_MAX_ROWS=2000`, `RETENTION_EXEMPT_EVENT_TYPES` reuse — consistent across P1/P3/P4. Branch names `feat/lean-pN-*` consistent. ✓

Execution: subagent-driven-development — fresh implementer per task/sub-PR, split spec/intent-then-code-quality reviews, gated PR per phase (P3 sub-PRs), CI authoritative via `gh pr checks`, the whole-suite single-process + order-flip gate before every merge, branch-hygiene + cross-session non-stomp (no engine-lane files, no stash/worktree touch) throughout. P5 excluded.
