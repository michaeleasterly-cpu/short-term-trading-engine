# Agents + Dev Environment — Phase A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]` checkboxes.

**Goal:** Codify the operator-endorsed development pipeline into a canonical `docs/DEV_PIPELINE_STANDARD.md`, guarded by an anti-rot presence-sentinel, with a CLAUDE.md pointer — so no future session silently drifts off it.

**Architecture:** Spec `docs/superpowers/specs/2026-05-19-agents-dev-environment-design.md` (v1, operator-approved). **Phase A only** (Pillar B): docs + one sentinel test + one CLAUDE.md paragraph — additive, zero cross-session collision, single-session window. **Phase B (Agent Teams adoption) is NOT in this plan** — it is design-only in the spec and a separately-scheduled, canary-first, operator-green-lit adoption tail. One gated PR.

**Tech Stack:** Markdown, pytest (`asyncio_mode=auto`), ruff. Subagent-driven; gated PR; CI authoritative via `gh pr checks`; the whole-suite single-process + order-flip authoritative gate (Lean P1 `-n auto --dist loadgroup` is accelerator-only); branch-hygiene (`git switch -c`, verify branch before every commit; no `git stash`).

**Reference (read, do not re-derive):** the spec §3 Phase A (A1–A4) — it fully specifies the doc's content; `docs/STYLE_GUIDE.md` (house doc style + the standard sits beside it); `CLAUDE.md` "Session Rules" register (where the A3 pointer goes); `tests/test_xdist_group_manifest.py` and `scripts/gen_engine_manifest.py` (the manifest/anti-rot discipline A4 mirrors); the merged Lean spec/plan + `pyproject.toml`/`.github/workflows/ci.yml`/`tests/test_xdist_group_manifest.py` (the Lean reality A2 documents — parallel accelerator vs serial+order-flip authoritative; tool-walk excludes; `respect-gitignore=false`).

**Standing acceptance gate for EVERY task:** the authoritative gate is the WHOLE single-process suite `python -m pytest -q` (0 failed; count ≥ current main baseline — re-measure at task start, currently ~1793) + order-flip `python -m pytest -p no:xdist -q tpcore/tests/test_ops.py tpcore/tests/test_ops_helpers.py tests/test_defect_register.py` AND reversed, both green. CI verified via `gh pr checks`, never `gh run watch`'s exit code.

---

## Phase A — Pillar B written standard (gated PR)

Branch `feat/dev-pipeline-standard` off fresh `main`.

**Files:** Create `docs/DEV_PIPELINE_STANDARD.md`, `tests/test_dev_pipeline_standard_present.py`; modify `CLAUDE.md`.

### Task A.1: anti-rot presence-sentinel (TDD — write the test FIRST)

- [ ] **Step 1 (failing test):** Create `tests/test_dev_pipeline_standard_present.py`:
```python
"""Anti-rot tripwire: docs/DEV_PIPELINE_STANDARD.md must exist and keep
its load-bearing clauses. Mirrors the gen_engine_manifest /
test_xdist_group_manifest manifest-discipline (Spec 2 Phase A4). This is
a PRESENCE check, NOT a behavioural test of the process (the process is
operator + reviewer discipline, un-testable here)."""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DOC = _REPO / "docs" / "DEV_PIPELINE_STANDARD.md"

# Load-bearing literal anchors — if any vanishes, the standard has been
# silently gutted; red CI. Keep this list == the spec §3 A4 anchors.
_ANCHORS = (
    "gh pr checks",
    "no:xdist",
    'xdist_group("ops_shadow")',
    "split-review",
    "git stash",
    "expert subagent",
    "spec-read gate",
    "order-flip",
)


def test_dev_pipeline_standard_present_and_intact() -> None:
    assert _DOC.is_file(), f"missing canonical standard: {_DOC}"
    src = _DOC.read_text()
    missing = [a for a in _ANCHORS if a not in src]
    assert not missing, (
        "DEV_PIPELINE_STANDARD.md lost load-bearing clauses "
        f"(silent rot): {missing}")
```
- [ ] **Step 2:** Run `python -m pytest tests/test_dev_pipeline_standard_present.py -q` → FAIL (`missing canonical standard`).

### Task A.2: write the canonical standard (makes the test pass)

- [ ] **Step 3:** Create `docs/DEV_PIPELINE_STANDARD.md` with these sections (content per spec §3 A1/A2 — concrete, no placeholders):

  **§1 The Pipeline (numbered, non-optional).** Exactly: 1. brainstorm; 2. commission a skeptical **expert subagent** to harden the design; 3. spec = its own gated docs-only PR; 4. operator **spec-read gate** (explicit human ack); 5. writing-plans = its own gated docs-only PR; 6. subagent-driven execution; 7. **SPLIT review** — dispatch a fresh-context spec/intent reviewer; ONLY on its PASS dispatch a SEPARATE fresh-context code-quality reviewer; never one combined two-gate reviewer; 8. implementer folds reviewer findings; 9. gated PR; 10. CI verified via **`gh pr checks <n>`** — never `gh run watch`'s exit code (documented misreport); 11. the whole single-process `pytest` + bidirectional module-**order-flip** is the AUTHORITATIVE gate (`-n auto --dist loadgroup` / `no:xdist` distinction — see §3); 12. squash-merge `--delete-branch`; 13. `git switch main && git pull` sync; 14. emit a paste-ready cross-session handoff message.

  **§2 Standing Discipline Rules** (each one line + the *why*): split-review separate dispatches (`feedback_split_review_dispatches`); `gh pr checks` not `gh run watch` (misreport incident); whole-suite + order-flip authoritative, never a subset (`ops/` package-shadow); ops-shadow `xdist_group("ops_shadow")` sentinel discipline — a new test touching `sys.modules['ops']` / `spec_from_file_location(ops)` / `importlib`-of-ops MUST carry the mark or `tests/test_xdist_group_manifest.py` reds CI; no `dashboard.py` import in a CI test (no `streamlit` in `pip install -e .[dev]`); cross-session non-stomp — never touch the other lane's files/worktrees, **no `git stash`** (repo-global, cross-session hazard); the snapshot/restore-`sys.modules['ops']` precedent **vs** the counter-rule that a `tpcore/tests/` test importing `ops.lab.run` uses a PLAIN import with NO `del sys.modules` eviction guard (both live; cite #148); `git switch` never `git checkout <sha|branch>`; one canonical cleanup `scripts/git_hygiene.sh`; backfills via `python scripts/ops.py --stage`, never one-off scripts.

  **§3 Lean Integration** (per spec A2): `pytest -n auto --dist loadgroup` is the FAST ACCELERATOR (local inner loop / CI speed); the serial + order-flip pair (the `ci.yml` "AUTHORITATIVE gate" step) is the GATE OF RECORD; the tool-walk excludes (`pyproject.toml` ruff `extend-exclude` + pytest `norecursedirs` + tracked `.ignore`, `respect-gitignore=false`) are why grep/ruff are fast — do NOT re-derive, do NOT re-enable `respect-gitignore`.

  **§4 Scope of this standard / pointer to Agent Teams.** One paragraph: this standard is topology-agnostic (holds under solo, two-session-relay, or future Agent Teams); Agent Teams adoption is the deferred Pillar A (see the spec) and does not change §1–§3.

  Ensure ALL `_ANCHORS` literals from A.1 appear verbatim in the prose (`gh pr checks`, `no:xdist`, `xdist_group("ops_shadow")`, `split-review`, `git stash`, `expert subagent`, `spec-read gate`, `order-flip`).
- [ ] **Step 4:** Run `python -m pytest tests/test_dev_pipeline_standard_present.py -q` → PASS.
- [ ] **Step 5:** Commit (`docs/DEV_PIPELINE_STANDARD.md` + `tests/test_dev_pipeline_standard_present.py`).

### Task A.3: CLAUDE.md pointer

- [ ] **Step 1:** In `CLAUDE.md`, in the **Session Rules** register, add one concise entry (match the existing bullet style), e.g.: `- **The canonical development pipeline is `docs/DEV_PIPELINE_STANDARD.md` — every non-trivial change follows it (brainstorm→expert-harden→spec/plan gated PRs→subagent exec→split review→`gh pr checks`→whole-suite+order-flip authoritative gate→squash-merge→handoff). Do not drift; the anti-rot sentinel `tests/test_dev_pipeline_standard_present.py` reds CI if the standard is gutted.**` Keep it one bullet; do not restructure CLAUDE.md.
- [ ] **Step 2:** Verify the whole-suite gate + the new sentinel still green; `ruff check tests/test_dev_pipeline_standard_present.py` clean. Commit.

### Task A.4: land

- [ ] **Step 1:** Full acceptance gate: parallel `python -m pytest -n auto --dist loadgroup -q` (0 failed) + AUTHORITATIVE serial `python -m pytest -q` (0 failed, count == parallel, = baseline+1 for the new sentinel) + both order-flips green; `ruff check tests/test_dev_pipeline_standard_present.py` clean; `git diff --name-only main..HEAD` = ONLY `docs/DEV_PIPELINE_STANDARD.md`, `tests/test_dev_pipeline_standard_present.py`, `CLAUDE.md`; `git stash list` unchanged (0).
- [ ] **Step 2:** Gated PR; split spec/intent then code-quality review; reviewer findings folded; CI authoritative via `gh pr checks`; squash-merge `--delete-branch`; `git switch main && git pull`.

---

## Self-Review

**1. Spec coverage:** spec §3 A1 (the pipeline doc + Standing Discipline Rules) → Task A.2 §1/§2; A2 (Lean integration) → A.2 §3; A3 (CLAUDE.md pointer) → Task A.3; A4 (anti-rot presence-sentinel) → Task A.1 (TDD-first). Spec §4 D2 (sentinel=yes) → A.1; D5 (CLAUDE.md pointer bundled into Phase A build) → A.3. Phase B (Agent Teams adoption) explicitly OUT of this plan per spec §6 — no task exists for it. Decisions D1/D3/D4 are Phase-B-adoption-time, correctly absent here. ✓

**2. Placeholder scan:** the doc content is concretely enumerated (§1 numbered steps verbatim, §2 rules verbatim, §3 the Lean facts, §4 the scope paragraph) + the exact `_ANCHORS` the sentinel enforces — no "fill in details". The CLAUDE.md bullet text is given verbatim. Every task has files + the actual test code + the doc structure + verify + commit + gated-PR. ✓

**3. Type/name consistency:** `docs/DEV_PIPELINE_STANDARD.md`, `tests/test_dev_pipeline_standard_present.py`, the `_ANCHORS` tuple (8 literals, == the doc prose), `test_dev_pipeline_standard_present_and_intact` — consistent A.1↔A.2↔A.3. Branch `feat/dev-pipeline-standard`. ✓

Execution: subagent-driven-development — fresh implementer, split spec/intent-then-code-quality reviews, gated PR, CI authoritative via `gh pr checks`, whole-suite + order-flip gate, branch-hygiene + no `git stash`. Phase B excluded (deferred adoption).
