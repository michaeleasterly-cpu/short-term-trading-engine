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


# ─── T6: ADD executor + readiness build gate (H-S3-11) ───


def test_add_new_scaffold_rejects_gate_fields():
    """H-S3-11(b): a new_scaffold engine cannot present a gate score it
    has not earned — non-None gate_dsr/gate_cred is a hard reject."""
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import attach_ecr_context, classify, validate
    ecr = EngineChangeRequest(
        action="add", engine="newx", source="new_scaffold",
        cadence="daily", allocator=False, dispatch_order=9,
        gate_dsr=0.99, gate_cred=80, need="x")
    plan = attach_ecr_context(classify(ecr, {}), ecr)
    vp = validate(plan, repo_root=None, ecr=ecr)
    assert vp.rejection is not None
    assert "new_scaffold" in vp.rejection and "gate" in vp.rejection


def test_add_always_lands_LAB():
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import classify
    ecr = EngineChangeRequest(
        action="add", engine="newx", source="new_scaffold",
        cadence="daily", allocator=True, dispatch_order=9, need="x")
    plan = classify(ecr, {})
    assert plan.to_state is LifecycleState.LAB  # never PAPER (H-S3-11a)


def test_add_always_lands_LAB_executor_hard_rejects_non_lab():
    """H-S3-11(a) non-vacuity: even if a tampered TransitionPlan asks the
    executor for to_state≠LAB, validate() HARD-REJECTS — an ADD can NEVER
    land PAPER/LIVE (that maturity is an automated transition, not ADD).
    A regression that let ADD land PAPER trips this directly."""
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import ApprovalClass, ECRAction, TransitionPlan, validate
    ecr = EngineChangeRequest(
        action="add", engine="newx", source="new_scaffold",
        cadence="daily", allocator=False, dispatch_order=9, need="x")
    forged = TransitionPlan(
        action=ECRAction.ADD, engine="newx", from_state=None,
        to_state=LifecycleState.PAPER,  # the forbidden landing state
        approval_class=ApprovalClass.OPERATOR, source="new_scaffold")
    vp = validate(forged, repo_root=None, ecr=ecr)
    assert vp.rejection is not None
    assert "ADD must land LAB" in vp.rejection


def test_add_lab_candidate_requires_promote_new(tmp_path):
    """H-S3-11(c): a lab_candidate ADD whose sidecar says fold_existing
    is a MODIFY, not an ADD — explicit redirect in the rejection."""
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import attach_ecr_context, classify, validate

    # build a fold_existing sidecar
    from tpcore.tests.test_lab_dossier_sidecar import _labresult
    r = _labresult()  # intent/recommended_exit == fold_existing
    md = tmp_path / "2026-05-18-revcand-SURVIVED-seed0.md"
    md.write_text("# rendered")
    md.with_suffix(".json").write_text(r.model_dump_json())
    ecr = EngineChangeRequest(
        action="add", engine="newx", source="lab_candidate",
        lab_dossier=str(md), cadence="daily", allocator=False,
        dispatch_order=9, need="x")
    plan = attach_ecr_context(classify(ecr, {}), ecr)
    vp = validate(plan, repo_root=None, ecr=ecr)
    assert vp.rejection is not None
    assert "fold_existing" in vp.rejection and "MODIFY" in vp.rejection


def test_add_lab_candidate_missing_sidecar_rejects(tmp_path):
    """H-S3-11(c) non-vacuity: a lab_candidate ADD whose .json sidecar
    does NOT exist is a loud reject (the T3 EvidenceError surfaced) — a
    regression that accepted a missing dossier trips this."""
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import attach_ecr_context, classify, validate
    md = tmp_path / "2026-05-18-nodossier-SURVIVED-seed0.md"
    md.write_text("# rendered")  # NO sibling .json
    ecr = EngineChangeRequest(
        action="add", engine="newx", source="lab_candidate",
        lab_dossier=str(md), cadence="daily", allocator=False,
        dispatch_order=9, need="x")
    plan = attach_ecr_context(classify(ecr, {}), ecr)
    vp = validate(plan, repo_root=None, ecr=ecr)
    assert vp.rejection is not None
    assert "sidecar" in vp.rejection


def test_add_lab_candidate_tampered_sidecar_rejects(tmp_path):
    """H-S3-11(c) non-vacuity: an extra-field (tampered) sidecar fails
    LabResult extra=forbid → loud reject, no mutation. Accepting a
    tampered dossier trips this."""
    import json

    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import attach_ecr_context, classify, validate
    from tpcore.tests.test_lab_dossier_sidecar import _labresult
    payload = json.loads(_labresult().model_dump_json())
    payload["smuggled_field"] = "tamper"  # extra=forbid → ValidationError
    md = tmp_path / "2026-05-18-tampered-SURVIVED-seed0.md"
    md.write_text("# rendered")
    md.with_suffix(".json").write_text(json.dumps(payload))
    ecr = EngineChangeRequest(
        action="add", engine="newx", source="lab_candidate",
        lab_dossier=str(md), cadence="daily", allocator=False,
        dispatch_order=9, need="x")
    plan = attach_ecr_context(classify(ecr, {}), ecr)
    vp = validate(plan, repo_root=None, ecr=ecr)
    assert vp.rejection is not None
    assert "tampered" in vp.rejection or "extra" in vp.rejection


def test_add_readiness_miss_rejects(tmp_path):
    """H-S3-11(d): a scaffold with no <engine>/tests dir / no
    BaseEnginePlug plugs ⇒ reject, zero mutation."""
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import apply, attach_ecr_context, classify
    staged = _make_synthetic_engine_tree(tmp_path)
    # remove the template so the scaffold is incomplete
    (staged / "tpcore" / "templates" / "engine_template").rename(
        staged / "tpcore" / "templates" / "_gone")
    ecr = EngineChangeRequest(
        action="add", engine="brandnew", source="new_scaffold",
        cadence="daily", allocator=False, dispatch_order=7, need="x")
    plan = attach_ecr_context(classify(ecr, {}), ecr)
    res = apply(plan, repo_root=staged, emit_audit=False)
    assert res.rejection is not None
    assert not (staged / "brandnew").is_dir(), "scaffold not cleaned up"


def test_add_leaves_frozen_literal_untouched(tmp_path):
    """H-S3-2 ADD leg: ADD → LAB does NOT change roster_for_dispatch()
    (LAB filtered by _DISPATCHABLE) — the frozen literal is unchanged."""
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import apply, attach_ecr_context, classify, validate
    staged = _make_synthetic_engine_tree(tmp_path)
    tc = (staged / "tpcore" / "tests"
          / "test_engine_lifecycle_consistency.py")
    before = tc.read_text()
    ecr = EngineChangeRequest(
        action="add", engine="brandnew", source="new_scaffold",
        cadence="daily", allocator=False, dispatch_order=7, need="x")
    plan = validate(attach_ecr_context(classify(ecr, {
        "reversion": LifecycleState.PAPER}), ecr),
        repo_root=staged, ecr=ecr)
    apply(plan, repo_root=staged, emit_audit=False)
    assert tc.read_text().count("roster_for_dispatch() == (") == \
        before.count("roster_for_dispatch() == ("), \
        "ADD→LAB must NOT touch the frozen literal"


def test_add_readiness_miss_rolls_back_to_byte_identical(tmp_path):
    """H-S3-4 ADD leg (readiness-reject path): a `new_scaffold` ADD from
    the bare engine_template legitimately fails the readiness gate (no
    `<engine>/tests/` dir — spec H-S3-11d; the template is a START point,
    the operator adds tests). The RuntimeError must reverse-replay the
    journaled scaffold-copy + _PROFILE write so the tree is BYTE-IDENTICAL
    — ZERO trace: no stray brandnew/, no _PROFILE entry. Proven with the
    T5 recursive-byte-map oracle so it is non-vacuous: scaffold residue,
    a surviving _PROFILE entry, or any drifted byte trips it."""
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import apply, attach_ecr_context, classify
    staged = _make_synthetic_engine_tree(tmp_path)
    ep = staged / "tpcore" / "engine_profile.py"
    pkg = staged / "brandnew"
    before_ep = ep.read_bytes()
    # oracle scoped to the new package subtree (T5 discipline — the whole
    # tpcore/ tree is noisy with subprocess-generated __pycache__ .pyc;
    # the ADD mutation surface is exactly brandnew/ + engine_profile.py).
    before_pkg = _snapshot_tree(pkg)
    assert not pkg.exists(), "brandnew/ must be absent pre-apply"
    ecr = EngineChangeRequest(
        action="add", engine="brandnew", source="new_scaffold",
        cadence="daily", allocator=False, dispatch_order=7, need="x")
    plan = attach_ecr_context(classify(ecr, {
        "reversion": LifecycleState.PAPER}), ecr)
    res = apply(plan, repo_root=staged, emit_audit=False)
    assert res.rejection is not None
    assert "readiness:" in res.rejection, res.rejection
    assert not pkg.exists(), (
        "failed ADD left a stray brandnew/ scaffold residue")
    assert ep.read_bytes() == before_ep, "engine_profile not byte-identical"
    assert '"brandnew"' not in ep.read_text(), (
        "failed ADD left a _PROFILE entry behind")
    after_pkg = _snapshot_tree(pkg)
    assert after_pkg == before_pkg, (
        "failed ADD did not restore the package subtree byte-identical — "
        f"extra={set(after_pkg) - set(before_pkg)} "
        f"missing={set(before_pkg) - set(after_pkg)}")


def test_add_red_consistency_rolls_back_to_byte_identical(tmp_path):
    """H-S3-4 ADD leg (post-stage clockwork-red path): with a scaffold
    that PASSES readiness (a `tests/` dir injected into the staged
    engine_template), the staged ADD→LAB still makes the consistency
    subprocess red (the SP1 `test_lab_sentinel_is_not_wired` pins exactly
    one LAB sentinel — a 2nd LAB engine is correctly a half-state until
    promoted). apply() must reverse-replay every journaled scaffold-copy
    + _PROFILE write to a BYTE-IDENTICAL pre-state — ZERO trace. Proven
    with the T5 recursive-byte-map oracle: scaffold residue, a surviving
    _PROFILE entry, or any drifted byte trips it (non-vacuous)."""
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import apply, attach_ecr_context, classify
    staged = _make_synthetic_engine_tree(tmp_path)
    # make the staged engine_template readiness-complete so the reject
    # comes from the post-stage subprocess, NOT the readiness gate.
    tmpl_tests = (staged / "tpcore" / "templates"
                  / "engine_template" / "tests")
    tmpl_tests.mkdir(parents=True)
    (tmpl_tests / "__init__.py").write_text("")
    (tmpl_tests / "test_smoke.py").write_text("def test_ok():\n    pass\n")
    ep = staged / "tpcore" / "engine_profile.py"
    pkg = staged / "brandnew"
    before_ep = ep.read_bytes()
    before_pkg = _snapshot_tree(pkg)  # T5 discipline (see sibling test)
    assert not pkg.exists(), "brandnew/ must be absent pre-apply"
    ecr = EngineChangeRequest(
        action="add", engine="brandnew", source="new_scaffold",
        cadence="daily", allocator=False, dispatch_order=7, need="x")
    plan = attach_ecr_context(classify(ecr, {
        "reversion": LifecycleState.PAPER}), ecr)
    res = apply(plan, repo_root=staged, emit_audit=False,
                _force_validate=True)
    assert res.rejection is not None
    assert "post-stage clockwork red" in res.rejection, res.rejection
    assert not pkg.exists(), (
        "failed ADD left a stray brandnew/ scaffold residue")
    assert ep.read_bytes() == before_ep, "engine_profile not byte-identical"
    assert '"brandnew"' not in ep.read_text(), (
        "failed ADD left a _PROFILE entry behind")
    after_pkg = _snapshot_tree(pkg)
    assert after_pkg == before_pkg, (
        "failed ADD did not restore the package subtree byte-identical — "
        f"extra={set(after_pkg) - set(before_pkg)} "
        f"missing={set(before_pkg) - set(after_pkg)}")


# ---- T7: MODIFY zero-trust + LAB->PAPER promote (H-S3-6) ----

def _modify_sidecar(tmp_path, *, target="reversion",
                     recommended="fold_existing", verdict="SURVIVED",
                     dsr=0.97, cred=64,
                     winning=None):
    from tpcore.tests.test_lab_dossier_sidecar import _labresult
    r = _labresult()
    d = r.model_dump()
    d["target_engine"] = target
    d["recommended_exit"] = recommended
    d["intent"] = recommended if recommended != "none" else "fold_existing"
    d["verdict"] = verdict
    d["dsr"] = dsr
    d["credibility_score"] = cred
    d["winning_params"] = winning or {"z_threshold": 3.1, "max_hold_days": 8}
    from tpcore.lab.models import LabResult
    r2 = LabResult.model_validate(d)
    md = tmp_path / "2026-05-18-revc-SURVIVED-seed0.md"
    md.write_text("# rendered")
    md.with_suffix(".json").write_text(r2.model_dump_json())
    return md


def _modify_ecr(md, **over):
    from ops.engine_sdlc.ecr import EngineChangeRequest
    kw = dict(action="modify", engine="reversion", lab_dossier=str(md),
              param_change={"z_threshold": "3.1", "max_hold_days": "8"},
              gate_dsr=0.97, gate_cred=64)
    kw.update(over)
    return EngineChangeRequest(**kw)


def test_modify_plan_sot_diff_is_always_empty(tmp_path):
    """H-S3-6(d) lifecycle-immutability: a MODIFY plan must carry ZERO
    _PROFILE edit -- strategy existence/lifecycle/allocator cannot be
    touched by MODIFY by construction."""
    from ops.engine_sdlc.planner import classify
    md = _modify_sidecar(tmp_path)
    plan = classify(_modify_ecr(md), {"reversion": LifecycleState.PAPER})
    forbidden = {"lifecycle_state", "allocator_eligible", "dispatch_order",
                 "cadence"}
    assert not (set(plan.sot_diff) & forbidden)
    assert plan.to_state == plan.from_state  # no lifecycle edge


def test_modify_rejects_forged_numbers(tmp_path):
    from ops.engine_sdlc.planner import attach_ecr_context, classify, validate
    md = _modify_sidecar(tmp_path, dsr=0.40)  # sidecar disagrees
    ecr = _modify_ecr(md, gate_dsr=0.97)
    vp = validate(attach_ecr_context(
        classify(ecr, {"reversion": LifecycleState.PAPER}), ecr),
        ecr=ecr)
    assert vp.rejection is not None and "dsr" in vp.rejection.lower()


def test_modify_rejects_wrong_target(tmp_path):
    from ops.engine_sdlc.planner import attach_ecr_context, classify, validate
    md = _modify_sidecar(tmp_path, target="vector")
    ecr = _modify_ecr(md, engine="reversion")
    vp = validate(attach_ecr_context(
        classify(ecr, {"reversion": LifecycleState.PAPER}), ecr),
        ecr=ecr)
    assert vp.rejection is not None and "target" in vp.rejection.lower()


def test_modify_rejects_non_param_ranges_key(tmp_path):
    from ops.engine_sdlc.planner import attach_ecr_context, classify, validate
    md = _modify_sidecar(tmp_path, winning={"not_a_real_param": 9})
    ecr = _modify_ecr(md, param_change={"not_a_real_param": "9"})
    vp = validate(attach_ecr_context(
        classify(ecr, {"reversion": LifecycleState.PAPER}), ecr),
        ecr=ecr)
    assert vp.rejection is not None
    assert "PARAM_RANGES" in vp.rejection or "not.*swept" in vp.rejection


def test_modify_rejects_value_mismatch(tmp_path):
    from ops.engine_sdlc.planner import attach_ecr_context, classify, validate
    md = _modify_sidecar(tmp_path, winning={"z_threshold": 9.9})
    ecr = _modify_ecr(md, param_change={"z_threshold": "3.1"})
    vp = validate(attach_ecr_context(
        classify(ecr, {"reversion": LifecycleState.PAPER}), ecr),
        ecr=ecr)
    assert vp.rejection is not None and "mismatch" in vp.rejection.lower()


def test_modify_rejects_promote_new_sidecar(tmp_path):
    """A promote_new dossier is an ADD, never a MODIFY (H-S3-6) -- even
    with SURVIVED/passing numbers it must hard-reject."""
    from ops.engine_sdlc.planner import attach_ecr_context, classify, validate
    md = _modify_sidecar(tmp_path, recommended="promote_new")
    ecr = _modify_ecr(md)
    vp = validate(attach_ecr_context(
        classify(ecr, {"reversion": LifecycleState.PAPER}), ecr),
        ecr=ecr)
    assert vp.rejection is not None
    assert "fold_existing" in vp.rejection


def test_modify_clean_sidecar_passes(tmp_path):
    """Non-vacuity guard: a clean fold_existing + SURVIVED + dsr>=0.95 +
    cred>=60 + in-PARAM_RANGES + value-matching + right-target sidecar
    must PASS (rejection is None) -- proves the gate is not a constant
    reject (mirrors T5's transient-proof discipline)."""
    from ops.engine_sdlc.planner import attach_ecr_context, classify, validate
    md = _modify_sidecar(tmp_path)
    ecr = _modify_ecr(md)
    vp = validate(attach_ecr_context(
        classify(ecr, {"reversion": LifecycleState.PAPER}), ecr),
        ecr=ecr)
    assert vp.rejection is None, vp.rejection
    assert vp.action.value == "modify"
    assert vp.to_state == vp.from_state  # no lifecycle edge


def test_modify_rejects_stale_sidecar(tmp_path):
    from ops.engine_sdlc.planner import attach_ecr_context, classify, validate
    # write a REAL valid sidecar at md's path so the reject below is
    # provably "the CITED path has no sidecar" (a different/stale
    # dossier), not "no sidecar exists anywhere" — the md side-effect is
    # the point; the value is intentionally unused.
    _modify_sidecar(tmp_path)
    # point the ECR at a DIFFERENT (nonexistent) dossier path
    ecr2 = _modify_ecr(tmp_path / "other-SURVIVED-seed9.md")
    vp = validate(attach_ecr_context(
        classify(ecr2, {"reversion": LifecycleState.PAPER}), ecr2),
        ecr=ecr2)
    assert vp.rejection is not None  # missing sidecar


def test_promote_flips_lab_to_paper_iff_gate_green(tmp_path):
    """LAB->PAPER is automated/gated (spec 4.1) -- not an ECR action.
    promote() flips iff the gate authority is green."""
    from ops.engine_sdlc.planner import promote
    staged = _make_synthetic_engine_tree(tmp_path)
    ep = staged / "tpcore" / "engine_profile.py"
    ep.write_text(ep.read_text().replace(
        '"throwaway", cadence=Cadence.DAILY,\n'
        '                               dispatch_order=6, '
        'lifecycle_state=LifecycleState.PAPER)',
        '"throwaway", cadence=Cadence.DAILY,\n'
        '                               dispatch_order=6, '
        'lifecycle_state=LifecycleState.LAB)'))
    before = _snapshot_tree(staged / "tpcore")
    res2 = promote("throwaway", repo_root=staged, emit_audit=False,
                   _gate_green=False)
    assert res2.rejection is not None  # gate red => no flip
    assert _snapshot_tree(staged / "tpcore") == before, (
        "a gate-red promote mutated the staged tree (must be a no-op)")
    assert "LifecycleState.LAB" in ep.read_text()  # still LAB
    res = promote("throwaway", repo_root=staged, emit_audit=False,
                  _gate_green=True)
    assert res.rejection is None, res.rejection
    assert "LifecycleState.PAPER" in ep.read_text()
    from ops.engine_sdlc.planner import _run_consistency_subprocess
    rc, out = _run_consistency_subprocess(staged)
    assert rc == 0, (
        f"promote LAB->PAPER must leave the clockwork GREEN:\n{out}")
