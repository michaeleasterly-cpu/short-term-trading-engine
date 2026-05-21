---
name: ops-package-shadow-full-suite-gate
description: "local/subset pytest green ≠ CI green: (a) ops/*.py `from ops import …` ↔ scripts/ops.py collision needs the WHOLE single-process suite; (b) CI venv lacks streamlit so a CI test must never import dashboard.py"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 2daba0e7-4abc-478f-b193-dae66fcbcce7
---

**Rule:** when a new `ops/<mod>.py` does `from ops import X`
(correct for production `python -m ops.<mod>`), its test MUST snapshot
+ restore `sys.modules['ops']` (and the `ops.*` keys it touches) — the
proven precedent is `tests/test_llm_triage_service.py:30-91`
(snapshot dict → make `ops` package-shaped → bind real siblings by
file path → `exec_module` → `finally:` restore exactly). A
purge-only block (delete shadowed `ops` then importlib-load) is
NOT enough — executing the new module's `from ops import …` re-registers
`sys.modules['ops']` and is never restored.

**Why:** `scripts/ops.py` is a single-file module that
`tpcore/tests/test_ops*.py` import as top-level `ops` (puts `scripts/`
on `sys.path`). The `ops/` directory is ALSO a package
(`engine_ladder`, `weekly_digest`, `defect_register`, …). In one
pytest process the two meanings of `import ops` poison each other;
which one wins is **collection-order dependent**. #254 DR1 shipped
green per implementer + BOTH reviewers ("1422 passed") yet CI went
red with ~40 `AttributeError: module 'ops' has no attribute …` in
test_ops/test_ops_helpers — because all three ran TARGETED SUBSETS,
not the whole single-process suite CI runs.

**How to apply:** (1) the acceptance gate for anything touching
`ops/*` or its tests is `python -m pytest -q` (the WHOLE suite, ONE
process, exactly as CI) + an explicit collection-order-flip check
(`test_ops* + new_test` AND reversed) — subset-green is not a gate.
(2) Put this in implementer AND reviewer dispatch prompts: "run the
full single-process suite, not a subset; subset-green ≠ CI-green for
ops/ package-shadow." (3) Reviewers verifying a suite count: a number
far below the true full count (1422 vs the real 1692) is itself a
tell the run was a subset. Pairs with
[[feedback_no_shortcuts_100_pct]] (verify the real gate, not a proxy)
and [[git-hygiene-method]] (test-isolation discipline).

**Counter-rule — a `tpcore/tests/` test that imports `ops.lab.run`
must NOT add a `del sys.modules[...]` eviction guard (SP-A2 T4,
2026-05-19, empirically proven).** The SP-A `test_lab_ntrials_ledger`
"collision-eviction stanza" pattern was being propagated into every
SP-A2 subagent prompt. T4's spec review reproduced, by counterfactual
injection into the full single-process suite: the literal
`del sys.modules["ops"|"ops.*"]` guard EVICTS the cached `ops`-package
modules that the SP2 oracle's *already-collected* `import
search_parameters as sp` shim resolves its `monkeypatch.setattr` targets
through → **2 SP2-oracle tests fail (`test_amain_smoke_survived_verdict`,
`test_amain_lab_path_namespaces_credibility`)**. The GUARD is the
perturbation, NOT the `ops.lab.run` import. The canonical SAFE pattern
for a tpcore test needing `ops.lab.run` is a **plain in-body
`import ops.lab.run` with NO eviction guard** — mirror
`tpcore/tests/test_lab_no_gate_poison.py:25` (green in the same full
suite). Verified robust by full single-process suite + order-flip both
directions (33/33). This is the SAME root-cause class as
[[research-llm-edge-discovery]]-adjacent task #148 (the pre-existing
`test_lab_ntrials_ledger.py` collection-time eviction defect): the
eviction guard, not the import, breaks isolation. **Apply:** in
implementer/reviewer prompts, when a tpcore test imports `ops.lab.run`,
mandate plain-import-no-guard + full-suite + order-flip; do NOT mandate
the eviction stanza (it is the OPPOSITE of the snapshot/restore rule
above, which is for an `ops/*.py` test doing `from ops import X` — keep
the two cases distinct).

**Companion rule — `pytestmark = pytest.mark.xdist_group("ops_shadow")`
on EVERY `sys.modules['ops']`-poisoning Lab/engine test (data-lane P1,
PR #102, merged to origin/main 2026-05-19; relayed cross-session).**
Orthogonal to (and combined WITH) the no-eviction-guard counter-rule:
the eviction-guard rule is per-worker (don't `del sys.modules`); the
xdist_group mark pins all ops-shadow-poisoning tests onto the SAME
xdist worker so parallel CI workers don't fight over the dual-meaning
`import ops`. #102 added the one-line mark (no logic change) to
`test_lab_credibility_pool_threaded.py`, `test_lab_isolation.py`,
`test_lab_ntrials_ledger.py`, `scripts/tests/test_lab_cli_entrypoint.py`.
A clockwork sentinel `tests/test_xdist_group_manifest.py` REDS CI if a
poisoning test lacks the mark. **SCOPE — CORRECTED (SP-A2 T5
code-review, empirically verified): the mark is required ONLY for the
`scripts/ops.py`-shadow poisoning pattern** the sentinel actually
matches — a test that directly references `sys.modules['ops']`, or
`spec_from_file_location`/`importlib.import_module` with an ops-path
segment (the snapshot/restore-precedent class above, incl. the
`test_ops*` files). A **plain dotted `import ops.lab.run`** installs
the *real* `ops` package (`__path__` present) — it is NOT the
non-package `scripts/ops.py` shadow; the sentinel regex does NOT match
it and it does NOT need the mark. Proof: the canonical mirror
`tpcore/tests/test_lab_no_gate_poison.py` uses plain dotted
`import ops.lab.run` and carries NO `pytestmark` on origin/main
post-#102, unflagged by the sentinel. So SP-A2 T5's
`test_lab_dsr_delivered.py` (plain dotted import, no-eviction-guard
pattern) correctly carries NO mark — do NOT add one; do NOT mandate
it in T6-T9 prompts for plain-dotted-`ops.lab.run` tests. (The earlier
over-broad "any test importing ops.lab.run MUST carry the mark" was
wrong — corrected here.) On rebase: keep BOTH the Lab change AND the mark (trivial
resolve). Data lane deferred its codebase-wide ruff --add-noqa /
vulture / orphan gates (P3a/b/d/e) to the post-session single window
so they don't stomp active Lab coding — engine lane: no action, just
awareness.

**Sibling instance — CI venv ≠ local dev venv (same parent class:
local-green ≠ CI-green):** `streamlit` is NOT a CI dependency (CI =
`pip install -e .[dev]`; streamlit is local-venv-only for running the
console). `dashboard.py` does `import streamlit` at module top — so
ANY test that imports `dashboard.py` (e.g. an `import_module(
"dashboard")` smoke) passes locally but fails CI with
`ModuleNotFoundError: No module named 'streamlit'`. #254 DR3 hit this
exactly (implementer + both reviewers green locally, CI red). **Rule:**
never import `dashboard.py` in a CI test — test the pure
`dashboard_components/<x>.py` module only (the established precedent is
`tests/test_dashboard_escalation.py`: pure-component tests, zero
`dashboard.py` import; the conftest sys.path note only makes the name
*resolve*, it does not install streamlit). General principle: a new
test must not import a module that needs a non-CI dependency — mirror
the nearest existing pure-component test precedent. Same fix-loop
discipline as the ops-shadow case: the real gate is CI itself
(`gh pr checks`, not local nor `gh run watch`'s exit code, which
misreported here too).
