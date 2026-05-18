"""SP3 planner — classify table + isolated-tree dry-run + AST-safe
rewrite (T4) and the ADD/REMOVE/MODIFY/promote executors (T5–T7 append
below). Lazy in-body import of ops.engine_sdlc (H-S3-10). SP3 test
files live in tpcore/tests/ (a collected pyproject testpath)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tpcore.engine_profile import LifecycleState


def _ecr(**kw):
    from ops.engine_sdlc.ecr import EngineChangeRequest
    return EngineChangeRequest(**kw)


def _snapshot():
    """The current real _PROFILE as the {engine: lifecycle_state} snapshot
    classify() consumes (a pure-arg snapshot — classify does NO I/O)."""
    from tpcore.engine_profile import _PROFILE
    return {k: p.lifecycle_state for k, p in _PROFILE.items()}


@pytest.mark.parametrize("action,engine,source,expect", [
    # ADD, engine absent → LAB, OPERATOR
    ("add", "newengine", "new_scaffold", ("LAB", "OPERATOR", None)),
    ("add", "newengine", "lab_candidate", ("LAB", "OPERATOR", None)),
    # ADD, engine present → reject
    ("add", "reversion", "new_scaffold", (None, None, "already exists")),
    # REMOVE present PAPER → RETIRED, OPERATOR
    ("remove", "sentinel", None, ("RETIRED", "OPERATOR", None)),
    # REMOVE absent → reject
    ("remove", "ghost", None, (None, None, "nothing to remove")),
    # REMOVE already-retired (sigma) → reject
    ("remove", "sigma", None, (None, None, "already retired")),
    # MODIFY present PAPER → unchanged, AUTOMATED
    ("modify", "reversion", None, ("PAPER", "AUTOMATED", None)),
    # MODIFY absent → reject
    ("modify", "ghost", None, (None, None, "nothing to modify")),
    # MODIFY retired (sigma) → reject
    ("modify", "sigma", None, (None, None, "cannot tune a retired")),
])
def test_classify_every_table_cell(action, engine, source, expect):
    from ops.engine_sdlc.planner import classify
    kw = {"action": action, "engine": engine}
    if action == "add":
        kw.update(source=source, cadence="daily", allocator=False,
                  dispatch_order=9, need="x")
    if action == "remove":
        kw.update(reason="x", eulogy_notes="x")
    if action == "modify":
        kw.update(lab_dossier="docs/lab/x.md",
                  param_change={"z_threshold": "3.1"},
                  gate_dsr=0.96, gate_cred=64)
    plan = classify(_ecr(**kw), _snapshot())
    exp_to, exp_appr, exp_reject = expect
    if exp_reject is not None:
        assert plan.rejection is not None
        assert exp_reject in plan.rejection
    else:
        assert plan.rejection is None
        assert plan.to_state == getattr(LifecycleState, exp_to)
        assert plan.approval_class == exp_appr


def test_profile_rewrite_adds_no_import():
    """H-S3-10: the _PROFILE rewrite changes ONLY EngineProfile(...) data
    tokens — it never adds an import/from line."""
    from ops.engine_sdlc.planner import _rewrite_profile_source
    src = Path("tpcore/engine_profile.py").read_text()
    new = _rewrite_profile_source(
        src, engine="reversion", set_state="retired",
        set_allocator_eligible=False)
    orig_imports = [ln for ln in src.splitlines()
                    if ln.startswith(("import ", "from "))]
    new_imports = [ln for ln in new.splitlines()
                   if ln.startswith(("import ", "from "))]
    assert new_imports == orig_imports, "the _PROFILE rewrite added/removed an import line"


def test_validate_runs_real_clockwork_in_isolated_tree(tmp_path):
    """H-S3-1 / D2: validate() stages the proposed tree into an isolated
    copytree and runs the REAL test_engine_lifecycle_consistency.py as a
    fresh subprocess with cwd=temp — a deliberately-introduced half-state
    must make validate reject with the clockwork's own failure text."""
    from ops.engine_sdlc.planner import _run_consistency_subprocess
    repo = Path(__file__).resolve().parents[2]
    staged = tmp_path / "tree"
    shutil.copytree(
        repo, staged,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "backtests"))
    # Introduce a half-state: flip reversion to RETIRED but DON'T move
    # the package / write an EULOGY — the clockwork must catch it.
    ep = staged / "tpcore" / "engine_profile.py"
    txt = ep.read_text().replace(
        'dispatch_order=1, lifecycle_state=LifecycleState.PAPER,\n'
        '                               allocator_eligible=True)',
        'dispatch_order=1, lifecycle_state=LifecycleState.RETIRED,\n'
        '                               allocator_eligible=False)')
    ep.write_text(txt)
    rc, out = _run_consistency_subprocess(staged)
    assert rc != 0, "a staged half-state must fail the real clockwork"
    assert "reversion" in out
