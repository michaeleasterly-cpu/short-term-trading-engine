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


# ─── T5: REMOVE executor + atomicity + completed archive-leg clockwork ───


def _make_synthetic_engine_tree(tmp_path: Path) -> Path:
    """copytree the repo, then add a synthetic PAPER engine `throwaway`
    so a REMOVE end-to-end can run entirely in a temp tree (tests never
    touch the working repo — standing rule)."""
    repo = Path(__file__).resolve().parents[2]
    staged = tmp_path / "tree"
    shutil.copytree(
        repo, staged,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "backtests"))
    # minimal real package + tests + scheduler so the live-engine leg
    # is satisfied before the retire.
    pkg = staged / "throwaway"
    (pkg / "tests").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "tests" / "__init__.py").write_text("")
    (pkg / "scheduler.py").write_text(
        "async def run_once(*a, **k):\n    return {}\n")
    # add a PAPER _PROFILE entry + the shadow tokens
    ep = staged / "tpcore" / "engine_profile.py"
    t = ep.read_text().replace(
        '    # allocator: separate _dispatch_allocator path',
        '    "throwaway": EngineProfile(engine="throwaway", '
        'cadence=Cadence.DAILY,\n'
        '                               dispatch_order=6, '
        'lifecycle_state=LifecycleState.PAPER),\n'
        '    # allocator: separate _dispatch_allocator path')
    ep.write_text(t)
    smoke = staged / "scripts" / "run_smoke_test.sh"
    smoke.write_text(smoke.read_text().replace(
        "for engine in reversion vector momentum sentinel canary; do",
        "for engine in reversion vector momentum sentinel canary "
        "throwaway; do"))
    pp = staged / "pyproject.toml"
    pj = pp.read_text().replace(
        '"canary*"]  # sigma archived 2026-05-16',
        '"canary*", "throwaway*"]  # sigma archived 2026-05-16').replace(
        '    "canary/tests",', '    "canary/tests",\n    "throwaway/tests",')
    pp.write_text(pj)
    # the frozen-literal pin must include throwaway BEFORE the retire so
    # the staged tree is green pre-REMOVE (H-S3-2: REMOVE then drops it).
    tc = staged / "tpcore" / "tests" / "test_engine_lifecycle_consistency.py"
    tc.write_text(tc.read_text().replace(
        '"reversion", "vector", "momentum", "sentinel", "canary")',
        '"reversion", "vector", "momentum", "sentinel", "canary", '
        '"throwaway")'))
    return staged


def test_clean_copytree_consistency_subprocess_passes(tmp_path):
    """Folded T4-review Minor (positive-direction D2): a CLEAN unmutated
    staged tree must make _run_consistency_subprocess return rc==0 — so
    a broken subprocess invocation that ALWAYS returns non-zero cannot
    masquerade as a working red-detector (the rc!=0 T4 test alone is
    satisfiable by a permanently-broken invocation)."""
    from ops.engine_sdlc.planner import _run_consistency_subprocess, _staged_copytree
    staged = _staged_copytree(tmp_path / "clean")
    rc, out = _run_consistency_subprocess(staged)
    assert rc == 0, f"a clean unmutated copytree must be green:\n{out}"


def test_remove_throwaway_engine_end_to_end(tmp_path):
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import ApprovalClass, apply, attach_ecr_context, classify, validate
    staged = _make_synthetic_engine_tree(tmp_path)
    ecr = EngineChangeRequest(
        action="remove", engine="throwaway",
        reason="synthetic test engine — never traded",
        eulogy_notes="exists only to prove the REMOVE executor")
    snap = {"reversion": LifecycleState.PAPER, "vector": LifecycleState.PAPER,
            "momentum": LifecycleState.PAPER, "sentinel": LifecycleState.PAPER,
            "canary": LifecycleState.PAPER, "throwaway": LifecycleState.PAPER,
            "allocator": LifecycleState.PAPER, "sigma": LifecycleState.RETIRED,
            "lab": LifecycleState.LAB}
    plan = attach_ecr_context(classify(ecr, snap), ecr)
    assert plan.rejection is None
    # TransitionPlan.approval_class is typed `str | None` (T4 schema): the
    # frozen pydantic model stores the StrEnum's .value, so the contract
    # is string-equality (the T4 oracle test_classify_every_table_cell
    # uses == too). `is` would assert identity the frozen model never
    # preserves — align to the real API, pinned behavior unchanged.
    assert plan.approval_class == ApprovalClass.OPERATOR
    vplan = validate(plan, repo_root=staged)
    assert vplan.rejection is None, vplan.rejection
    apply(vplan, repo_root=staged, emit_audit=False)
    # post-conditions: package moved, EULOGY written with the content
    # floor, _PROFILE flipped, the extended clockwork passes on the tree.
    assert not (staged / "throwaway").is_dir()
    eulogy = staged / "archive" / "throwaway" / "EULOGY.md"
    assert eulogy.is_file()
    body = eulogy.read_text()
    assert "## Cause of death" in body
    assert "## Retirement checklist" in body
    assert "synthetic test engine" in body
    from ops.engine_sdlc.planner import _run_consistency_subprocess
    rc, out = _run_consistency_subprocess(staged)
    assert rc == 0, f"clean retire must leave the clockwork green:\n{out}"


def _snapshot_tree(*roots: Path) -> dict[str, bytes]:
    """Full recursive {posix-relpath-under-its-root: bytes} map of every
    file under each root (a root that doesn't exist contributes nothing
    but is keyed so its later appearance/absence is observable). This is
    the byte-identical oracle: a stray EULOGY, a file stranded in
    archive/<engine>/, a half-gutted package, or a drifted text edit all
    change this map (closes #I1 — the prior subset assertions stayed
    green while #C1 was live)."""
    snap: dict[str, bytes] = {}
    for r in roots:
        if not r.exists():
            continue
        for p in sorted(r.rglob("*")):
            if p.is_file():
                snap[f"{r.name}::{p.relative_to(r).as_posix()}"] = (
                    p.read_bytes())
    return snap


def test_apply_red_consistency_rolls_back_to_byte_identical(tmp_path):
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import apply, attach_ecr_context, classify
    staged = _make_synthetic_engine_tree(tmp_path)
    ep = staged / "tpcore" / "engine_profile.py"
    smoke = staged / "scripts" / "run_smoke_test.sh"
    pp = staged / "pyproject.toml"
    cg = staged / "tpcore" / "quality" / "validation" / "capital_gate.py"
    tc = staged / "tpcore" / "tests" / "test_engine_lifecycle_consistency.py"
    pkg = staged / "throwaway"
    arc = staged / "archive" / "throwaway"
    # FULL recursive pre-state of every path apply() can touch: the whole
    # package, both shadows, _PROFILE, capital_gate, the frozen-literal
    # test, and the (absent) archive dir. Post-rollback this map MUST be
    # byte-identical — any stray/missing/drifted byte trips it (#I1).
    before_files = {p: p.read_bytes()
                    for p in (ep, smoke, pp, cg, tc)}
    before_pkg = _snapshot_tree(pkg, arc)
    assert not arc.exists(), "archive/<engine>/ must be absent pre-apply"
    ecr = EngineChangeRequest(
        action="remove", engine="throwaway", reason="x", eulogy_notes="y")
    snap = {"throwaway": LifecycleState.PAPER}
    plan = attach_ecr_context(classify(ecr, snap), ecr)
    # Force a red apply: corrupt the consistency test in the staged tree
    # so the post-stage subprocess exits non-zero — apply must restore
    # every journaled file byte-identical and move nothing permanently.
    # (The corruption is appended; the journal snapshot of `tc` was taken
    # by _maybe_rewrite_frozen_literal BEFORE this line via record_file,
    # so the byte-oracle below uses the WITH-corruption baseline for tc.)
    tc.write_text(tc.read_text() +
                  "\n\ndef test_forced_red():\n    assert False\n")
    before_files[tc] = tc.read_bytes()  # post-corruption tc is the pre-apply state
    res = apply(plan, repo_root=staged, emit_audit=False,
                _force_validate=True)
    assert res.rejection is not None
    assert "post-stage clockwork red" in res.rejection, res.rejection
    # byte-identical reversal of EVERY touched path:
    for p, b in before_files.items():
        assert p.read_bytes() == b, f"{p.name} not restored byte-identical"
    after_pkg = _snapshot_tree(pkg, arc)
    assert after_pkg == before_pkg, (
        "package not restored to EXACT recursive file-set/bytes — "
        f"extra={set(after_pkg) - set(before_pkg)} "
        f"missing={set(before_pkg) - set(after_pkg)}")
    assert pkg.is_dir(), "package move not reverted"
    # the stray-EULOGY (#C1) would appear here:
    assert not (pkg / "EULOGY.md").exists(), (
        "#C1 regression: a generated EULOGY survived inside the package")
    assert sorted(p.name for p in pkg.iterdir()) == [
        "__init__.py", "scheduler.py", "tests"], (
        "restored package contents drifted from the original file-set")
    # nothing stranded in archive/<engine>/ (#C2 leak):
    assert not arc.exists(), "archive/<engine>/ left behind after rollback"


def test_apply_mid_move_loop_failure_byte_identical(tmp_path, monkeypatch):
    """#I2/#C2: inject a failure mid-way through the per-item package
    move loop (1st item already relocated into archive/, journal holds
    that move) — the realistic window the old arc.mkdir injection never
    reached. With per-item journaled-before-move, restore() reverses the
    completed move(s) exactly: the package returns byte-identical with
    NO stray EULOGY and NO file stranded in archive/, and the loud
    escalation (rejection carrying the inner exception) surfaces.

    Pre-refactor (#C2) this would strand the 1st item in archive/ with
    an EMPTY journal (the coarse (pkg,arc) tuple was appended only AFTER
    the whole loop) — restore() was a no-op and this assertion trips."""
    from ops.engine_sdlc import planner as P
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import apply, attach_ecr_context, classify
    staged = _make_synthetic_engine_tree(tmp_path)
    ep = staged / "tpcore" / "engine_profile.py"
    smoke = staged / "scripts" / "run_smoke_test.sh"
    pp = staged / "pyproject.toml"
    cg = staged / "tpcore" / "quality" / "validation" / "capital_gate.py"
    tc = staged / "tpcore" / "tests" / "test_engine_lifecycle_consistency.py"
    pkg = staged / "throwaway"
    arc = staged / "archive" / "throwaway"
    before_files = {p: p.read_bytes() for p in (ep, smoke, pp, cg, tc)}
    before_pkg = _snapshot_tree(pkg, arc)
    assert not arc.exists()
    # Patch shutil.move so the 2nd package-item move raises — by then the
    # 1st item is already in archive/ AND (move loop runs before eulogy)
    # at least one journaled move is recorded. Then also force a raise
    # right after the eulogy write so the failure window spans both.
    real_move = P.shutil.move
    calls = {"n": 0}

    def flaky_move(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("injected mid-move-loop failure")
        return real_move(src, dst)

    monkeypatch.setattr(P.shutil, "move", flaky_move)
    ecr = EngineChangeRequest(
        action="remove", engine="throwaway", reason="x", eulogy_notes="y")
    res = apply(attach_ecr_context(
                    classify(ecr, {"throwaway": LifecycleState.PAPER}), ecr),
                repo_root=staged, emit_audit=False)
    assert res.rejection is not None
    assert "apply aborted" in res.rejection, res.rejection
    assert "injected mid-move-loop failure" in res.rejection, (
        "the inner exception was swallowed, not escalated loudly")
    for p, b in before_files.items():
        assert p.read_bytes() == b, f"{p.name} not reverted byte-identical"
    after_pkg = _snapshot_tree(pkg, arc)
    assert after_pkg == before_pkg, (
        "#C2: mid-move-loop failure left the package non-byte-identical — "
        f"extra={set(after_pkg) - set(before_pkg)} "
        f"missing={set(before_pkg) - set(after_pkg)}")
    assert pkg.is_dir() and sorted(p.name for p in pkg.iterdir()) == [
        "__init__.py", "scheduler.py", "tests"]
    assert not (pkg / "EULOGY.md").exists(), "#C1: stray EULOGY in package"
    assert not arc.exists(), "#C2: files stranded in archive/<engine>/"


def test_apply_restore_failure_escalates_loudly(tmp_path, monkeypatch):
    """The loud-escalation invariant must survive the refactor: if
    _Journal.restore() itself raises, apply() returns
    outcome-equivalent rejection carrying the INNER restore exception
    (not the original, not swallowed) — proven via a no-op/raising
    restore monkeypatch (the technique that keeps these non-vacuous)."""
    from ops.engine_sdlc import planner as P
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import apply, attach_ecr_context, classify
    staged = _make_synthetic_engine_tree(tmp_path)
    (staged / "archive").mkdir(parents=True, exist_ok=True)
    (staged / "archive" / "throwaway").write_text("not-a-dir")  # forces raise

    def boom(self):
        raise RuntimeError("restore detonated")

    monkeypatch.setattr(P._Journal, "restore", boom)
    ecr = EngineChangeRequest(
        action="remove", engine="throwaway", reason="x", eulogy_notes="y")
    res = apply(attach_ecr_context(
                    classify(ecr, {"throwaway": LifecycleState.PAPER}), ecr),
                repo_root=staged, emit_audit=False)
    assert res.rejection is not None
    assert "restore detonated" in res.rejection, (
        "restore-failure exception was swallowed, not escalated")


def test_apply_move_failure_restores_text_edits(tmp_path):
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import apply, attach_ecr_context, classify
    staged = _make_synthetic_engine_tree(tmp_path)
    ep = staged / "tpcore" / "engine_profile.py"
    smoke = staged / "scripts" / "run_smoke_test.sh"
    before_ep = ep.read_bytes()
    before_smoke = smoke.read_bytes()
    # Pre-create archive/throwaway as a FILE so _apply_remove's
    # `arc.mkdir(exist_ok=True)` raises FileExistsError AFTER the cg /
    # shadow / frozen-literal TEXT edits are written but before the
    # _PROFILE flip + package move — the exception path must revert
    # every journaled text edit byte-identical (non-vacuous: a no-op
    # rollback would leave the shadow edits and trip the assertions).
    (staged / "archive").mkdir(parents=True, exist_ok=True)
    (staged / "archive" / "throwaway").write_text("not-a-dir")
    ecr = EngineChangeRequest(
        action="remove", engine="throwaway", reason="x", eulogy_notes="y")
    res = apply(attach_ecr_context(
                    classify(ecr, {"throwaway": LifecycleState.PAPER}), ecr),
                repo_root=staged, emit_audit=False)
    assert res.rejection is not None
    assert "apply aborted" in res.rejection, res.rejection
    assert ep.read_bytes() == before_ep, "text edits not reverted on move failure"
    assert smoke.read_bytes() == before_smoke, (
        "shadow edit (written before the raise) not reverted on apply abort")


def test_profile_rewrite_is_ast_valid_and_preserves_siblings():
    import ast

    from ops.engine_sdlc.planner import _rewrite_profile_source
    src = Path("tpcore/engine_profile.py").read_text()
    new = _rewrite_profile_source(
        src, engine="sentinel", set_state="retired",
        set_allocator_eligible=False)
    ast.parse(new)  # AST-valid
    # siblings untouched: reversion's line is byte-identical
    assert ('"reversion": EngineProfile(engine="reversion"' in new)
    assert "lifecycle_state=LifecycleState.RETIRED" in new
    # the comments are preserved
    assert "# allocator: separate _dispatch_allocator path" in new


def test_malformed_rewrite_aborts_with_zero_disk_change(tmp_path):
    from ops.engine_sdlc.planner import _rewrite_profile_source
    with pytest.raises(ValueError, match="not found"):
        _rewrite_profile_source(
            "x = 1\n", engine="nope", set_state="retired",
            set_allocator_eligible=False)


def test_remove_rostered_engine_updates_frozen_literal(tmp_path):
    """H-S3-2 REMOVE leg: removing a CURRENTLY-ROSTERED engine changes
    roster_for_dispatch(), so the planner mechanically rewrites the
    test_dispatch_order_invariant_is_the_frozen_literal tuple in the
    SAME staged diff — never a hand-edit."""
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import apply, attach_ecr_context, classify, validate
    staged = _make_synthetic_engine_tree(tmp_path)
    ecr = EngineChangeRequest(
        action="remove", engine="throwaway", reason="x", eulogy_notes="y")
    plan = validate(attach_ecr_context(
                        classify(ecr, {"throwaway": LifecycleState.PAPER}),
                        ecr),
                     repo_root=staged)
    apply(plan, repo_root=staged, emit_audit=False)
    tc = (staged / "tpcore" / "tests"
          / "test_engine_lifecycle_consistency.py").read_text()
    assert '"throwaway")' not in tc, (
        "the frozen-literal was not updated to drop the retired engine")
    assert '"reversion", "vector", "momentum", "sentinel", "canary")' in tc
