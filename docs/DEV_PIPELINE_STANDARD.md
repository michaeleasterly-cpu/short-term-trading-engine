# Dev Pipeline Standard

This document is the canonical, operator-endorsed development pipeline for this repository. It sits beside `docs/STYLE_GUIDE.md`: STYLE_GUIDE.md governs how code looks; this governs how a change moves from idea to merged.

Both Claude Code sessions converged on this pipeline and the operator explicitly endorsed it (2026-05-19). It lived only in session memory and habit; nothing canonical encoded it, so a future session could silently drift off it. This document removes that risk. The anti-rot presence-sentinel `tests/test_dev_pipeline_standard_present.py` reds CI if this standard is deleted or its load-bearing clauses are gutted — the same manifest-discipline mechanism the repo already trusts (`scripts/gen_engine_manifest.py` / `tests/test_xdist_group_manifest.py`).

This standard is **descriptive of the process**, not a substitute for the operator and reviewer discipline that enforces it. The sentinel checks that the standard is present and intact; it cannot test that the process was followed.

## §1 The Pipeline (numbered, non-optional)

Every non-trivial change follows these steps in order. None is optional.

1. **Brainstorm.** Explore intent, requirements, and design before touching code.
2. **Commission a skeptical expert subagent to harden the design.** A fresh-context expert subagent stress-tests the design, surfaces fatal objections, and tightens scope. The expert decides design/approach — do not over-ask the operator.
3. **Spec = its own gated docs-only PR.** The hardened design is written as a spec and lands as a separate, docs-only gated PR.
4. **Operator spec-read gate.** An explicit human acknowledgement that the operator has read the spec. This is a `spec-read gate` — a hard checkpoint, not a courtesy.
5. **Writing-plans = its own gated docs-only PR.** The implementation plan is written and lands as a separate, docs-only gated PR.
6. **Subagent-driven execution.** A fresh implementer subagent executes the plan task by task.
7. **SPLIT review.** Dispatch a fresh-context spec/intent reviewer. ONLY on its PASS, dispatch a SEPARATE fresh-context code-quality reviewer. Never one combined two-gate reviewer. This is the `split-review` rule: spec-compliance then, on PASS, a distinct fresh-context code-quality pass.
8. **Implementer folds reviewer findings.** The implementer (not the reviewer) applies the review findings.
9. **Gated PR.** The change lands as a gated pull request.
10. **CI verified via `gh pr checks <n>`** — never `gh run watch`'s exit code. `gh run watch` has a documented misreport: its exit code does not faithfully reflect check status. Read `gh pr checks <n>` instead.
11. **The whole single-process `pytest` + bidirectional module-order-flip is the AUTHORITATIVE gate.** The fast parallel run (`-n auto --dist loadgroup`) is an accelerator only; the serial run plus the reversed-module-order run is the gate of record (see §3 for the `-n auto --dist loadgroup` / `no:xdist` distinction).
12. **Squash-merge `--delete-branch`.**
13. **`git switch main && git pull` sync.**
14. **Emit a paste-ready cross-session handoff message.** A copy-paste-ready handoff so the other lane can pick up cleanly.

## §2 Standing Discipline Rules

Each rule with its *why*. These are repeatedly-violated failure modes; they are non-negotiable.

- **Split-review separate dispatches.** Dispatch a fresh-context spec/intent reviewer; only on its PASS dispatch a SEPARATE fresh-context code-quality reviewer; never one combined two-gate reviewer. *Why:* a combined reviewer dilutes both gates and lets code-quality slip through on spec PASS (`feedback_split_review_dispatches`).
- **`gh pr checks` not `gh run watch`.** Verify CI via `gh pr checks <n>`. *Why:* `gh run watch`'s exit code is a documented misreport — it can return success while checks are red.
- **Whole-suite + order-flip is authoritative, never a subset.** Run the entire suite in one process plus the reversed module order; a subset-green result is not CI-green. *Why:* the `ops/` package-shadow — module import order and `sys.modules['ops']` state make subset runs unrepresentative of CI.
- **Ops-shadow `xdist_group("ops_shadow")` sentinel discipline.** A new test that touches `sys.modules['ops']`, `spec_from_file_location(ops)`, or `importlib`-of-ops MUST carry the `xdist_group("ops_shadow")` mark. *Why:* without the mark `tests/test_xdist_group_manifest.py` reds CI, and the test can race other ops-touching tests under parallel execution.
- **No `dashboard.py` import in a CI test.** *Why:* `streamlit` is not in `pip install -e .[dev]`; importing `dashboard.py` in a CI test breaks collection.
- **Cross-session non-stomp; no `git stash`.** Never touch the other lane's files or worktrees. Never use `git stash`. *Why:* `git stash` is repo-global — it is a cross-session hazard that silently moves the other lane's working tree. Restore transient edits via `git checkout -- <path>`.
- **The snapshot/restore-`sys.modules['ops']` precedent vs the plain-import counter-rule (both live).** Some tests snapshot and restore `sys.modules['ops']`; a `tpcore/tests/` test importing `ops.lab.run` uses a PLAIN import with NO `del sys.modules` eviction guard. Both patterns are live and both are cited; do not "unify" them (see `#148`, engine-lane-tracked — do not fix opportunistically).
- **`git switch` never `git checkout <sha|branch>`.** *Why:* `git switch` refuses to silently detach HEAD; the checkout-detach incident is why.
- **One canonical cleanup: `scripts/git_hygiene.sh`.** *Why:* no ad-hoc destructive git anywhere; one reproducible mechanism.
- **Backfills via `python scripts/ops.py --stage`, never one-off scripts.** *Why:* one-off scripts drift from the canonical handler's validation/archive/idempotency guarantees and accrete into an unmaintainable rat's nest.

## §3 Lean Integration

The Lean Dev Env work (spec 1 of 2) added a fast test path and tool-walk excludes. This subsection records what is the accelerator and what is the gate of record so no future session re-derives or re-enables what was deliberately disabled.

- **`pytest -n auto --dist loadgroup` is the FAST ACCELERATOR.** It is for the local inner loop and CI speed only. It is NOT the gate.
- **The serial + order-flip pair is the GATE OF RECORD.** The `ci.yml` "AUTHORITATIVE gate" step runs the whole suite serially plus the reversed module order. A green parallel run with a red serial/order-flip run is a FAIL.
- **The tool-walk excludes are why grep/ruff are fast — do not re-derive, do not re-enable.** The `pyproject.toml` ruff `extend-exclude`, the pytest `norecursedirs`, the tracked `.ignore` file, and `respect-gitignore=false` are deliberate. In particular, do NOT re-enable `respect-gitignore`; the tracked `.ignore` is the intended walk-exclusion source of truth.

## §4 Scope of this standard / pointer to Agent Teams

This standard is **topology-agnostic**: it holds unchanged whether the work is done by a single session, the two-session human-relay (the current cross-session coordination), or a future Agent Teams topology. The pipeline (§1), the Standing Discipline Rules (§2), and the Lean integration (§3) do not change with the coordination mechanism — Agent Teams reorganize who types, not the authoritative gate.

Agent Teams adoption is the deferred **Pillar A** (see the spec `docs/superpowers/specs/2026-05-19-agents-dev-environment-design.md` §3 Phase B). Its adoption is gated, canary-one-task-first, and operator-green-lit; it is explicitly **out of scope for this standard** and does not modify §1–§3.
