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


def test_run_consistency_subprocess_catches_staged_half_state(tmp_path):
    """H-S3-1 / D2 (the direct-subprocess leg, retained): a
    deliberately-introduced half-state in a staged copytree must make the
    REAL test_engine_lifecycle_consistency.py subprocess fail (rc≠0) with
    the clockwork's own failure text. Pins _run_consistency_subprocess
    itself; the validate()-level proof is the next test."""
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


def test_validate_runs_real_clockwork_in_isolated_tree(tmp_path):
    """BLOCKER 1 / spec §3.2 + §5.2 step 2 + H-S3-1 + H-S3-7(b):
    ``validate()`` itself MUST run the spec-mandated PRE-APPROVAL
    isolated dry consistency run — copytree the proposed tree, stage the
    SAME edits apply() would write, run the REAL clockwork as a fresh
    subprocess — and set ``.rejection`` (with the clockwork's own failure
    text) on red, BEFORE the operator y/n.

    NON-VACUOUS / transient-break proof: the staged synthetic tree's
    frozen-literal pin is deliberately corrupted (an entry the SoT does
    not justify) so the proposed REMOVE diff makes the REAL clockwork
    RED. A ``validate()`` that SKIPS the dry run (or fabricates GREEN)
    returns ``.rejection is None`` and this assertion fails — i.e.
    deleting the ``_dry_consistency_run`` gate in ``validate()`` trips
    this test. Paired with ``test_validate_clean_diff_dry_run_passes``
    (a clean diff → ``.rejection is None``) so a constant-reject
    validate cannot satisfy both."""
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import (
        attach_ecr_context,
        classify,
        validate,
    )
    staged = _make_synthetic_engine_tree(tmp_path)
    # Corrupt the frozen-literal pin in the staged tree so that the
    # proposed REMOVE of `throwaway` (which _maybe_rewrite_frozen_literal
    # only drops `throwaway`) leaves a literal that no longer matches the
    # SoT roster — the REAL clockwork's
    # test_dispatch_order_invariant_is_the_frozen_literal goes RED.
    tc = (staged / "tpcore" / "tests"
          / "test_engine_lifecycle_consistency.py")
    tc.write_text(tc.read_text().replace(
        '"reversion", "vector", "momentum", "sentinel", "canary", '
        '"throwaway")',
        '"reversion", "vector", "momentum", "sentinel", "canary", '
        '"throwaway", "ghost_never_in_sot")'))
    ecr = EngineChangeRequest(
        action="remove", engine="throwaway",
        reason="synthetic — proves validate() runs the dry clockwork",
        eulogy_notes="x")
    snap = {"reversion": LifecycleState.PAPER,
            "vector": LifecycleState.PAPER,
            "momentum": LifecycleState.PAPER,
            "sentinel": LifecycleState.PAPER,
            "canary": LifecycleState.PAPER,
            "throwaway": LifecycleState.PAPER,
            "allocator": LifecycleState.PAPER,
            "sigma": LifecycleState.RETIRED, "lab": LifecycleState.LAB}
    plan = attach_ecr_context(classify(ecr, snap), ecr)
    assert plan.rejection is None  # classify is happy — the dry run gates
    vplan = validate(plan, repo_root=staged, ecr=ecr)
    assert vplan.rejection is not None, (
        "validate() did NOT run the spec-mandated pre-approval dry "
        "consistency run — a staged clockwork-red diff slipped through "
        "(BLOCKER 1: a skipped dry run / fabricated GREEN)")
    assert "dry consistency run RED" in vplan.rejection
    # the clockwork's OWN failure text is surfaced (not a fabricated msg)
    assert ("frozen" in vplan.rejection
            or "roster_for_dispatch" in vplan.rejection
            or "ghost_never_in_sot" in vplan.rejection), vplan.rejection
    # the REAL repo was never touched by validate()'s dry run.
    assert (Path(__file__).resolve().parents[2] / "throwaway").exists() \
        is False


def test_validate_clean_diff_dry_run_passes(tmp_path):
    """BLOCKER 1 positive leg (non-vacuity pair): a CLEAN REMOVE whose
    proposed diff IS consistent must make validate()'s pre-approval dry
    run PASS — ``.rejection is None``. So a constant-reject (or a
    permanently-broken subprocess invocation) validate cannot satisfy
    both this and the red test above (the T5 transient-proof discipline,
    applied at the validate() layer)."""
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import (
        attach_ecr_context,
        classify,
        validate,
    )
    staged = _make_synthetic_engine_tree(tmp_path)
    ecr = EngineChangeRequest(
        action="remove", engine="throwaway",
        reason="synthetic — a clean consistent REMOVE", eulogy_notes="x")
    snap = {"reversion": LifecycleState.PAPER,
            "vector": LifecycleState.PAPER,
            "momentum": LifecycleState.PAPER,
            "sentinel": LifecycleState.PAPER,
            "canary": LifecycleState.PAPER,
            "throwaway": LifecycleState.PAPER,
            "allocator": LifecycleState.PAPER,
            "sigma": LifecycleState.RETIRED, "lab": LifecycleState.LAB}
    plan = attach_ecr_context(classify(ecr, snap), ecr)
    vplan = validate(plan, repo_root=staged, ecr=ecr)
    assert vplan.rejection is None, (
        f"a clean consistent REMOVE diff must pass validate()'s "
        f"pre-approval dry run: {vplan.rejection}")


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

    # build a fold_existing sidecar at an IDENTITY-FRESH path (spec
    # §5.4/H-S3-6b is now enforced first; the path tokens must match the
    # sidecar so this test still exercises the fold_existing→MODIFY
    # redirect it is about, not the new identity gate).
    from tpcore.tests.test_lab_dossier_sidecar import _labresult
    r = _labresult()  # intent/recommended_exit == fold_existing
    day = r.generated_at.strftime("%Y-%m-%d")
    md = tmp_path / f"{day}-{r.candidate}-{r.verdict}-seed{r.seed}.md"
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
    # The dossier filename MUST be identity-fresh (spec §5.4 / H-S3-6b):
    # the real ops/lab/dossier.py format is
    #   {generated_at:%Y-%m-%d}-{candidate}-{verdict}-seed{seed}.md
    # _labresult() ⇒ candidate=rev_cand, seed=0, generated_at=2026-05-18.
    # A path whose tokens disagree with the sidecar is now a hard reject
    # (BLOCKER 2), so build the matching name from the model itself.
    day = r2.generated_at.strftime("%Y-%m-%d")
    md = tmp_path / f"{day}-{r2.candidate}-{r2.verdict}-seed{r2.seed}.md"
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


def _identity_mismatched_sidecar(tmp_path, *, target="reversion"):
    """A perfectly-VALID SURVIVED fold_existing sidecar (passes every
    zero-trust number) whose ``candidate``/``seed`` DISAGREE with the
    cited dossier filename — the spec §5.4/H-S3-6b "a valid sidecar from
    a DIFFERENT Lab run sitting at the cited path is a hard reject"
    case. Returns the cited md path (its .json is the wrong-run sidecar);
    the path's tokens are deliberately a real Lab dossier name for a
    DIFFERENT (candidate, seed)."""
    from tpcore.lab.models import LabResult
    from tpcore.tests.test_lab_dossier_sidecar import _labresult
    r = _labresult()  # candidate=rev_cand, seed=0, SURVIVED
    d = r.model_dump()
    d["target_engine"] = target
    d["winning_params"] = {"z_threshold": 3.1, "max_hold_days": 8}
    sidecar_lr = LabResult.model_validate(d)  # candidate rev_cand / seed 0
    # the CITED filename names a DIFFERENT run (candidate + seed differ)
    # but is a structurally-valid Lab dossier name.
    md = tmp_path / "2026-05-18-other_candidate-SURVIVED-seed42.md"
    md.write_text("# rendered")
    md.with_suffix(".json").write_text(sidecar_lr.model_dump_json())
    return md


def test_modify_rejects_identity_mismatched_sidecar(tmp_path):
    """BLOCKER 2 (spec §5.4 / H-S3-6b): a VALID SURVIVED fold_existing
    sidecar whose candidate/seed differ from the ECR's CITED dossier
    path is a HARD reject — a sidecar for a DIFFERENT Lab run cannot be
    laundered through the cited path.

    NON-VACUOUS: every gate NUMBER passes (SURVIVED/dsr0.97/cred64/
    fold_existing/right-target/in-PARAM_RANGES/value-match), so the ONLY
    thing that can reject is the identity-freshness check. Removing
    ``assert_identity_fresh`` from ``_validate_modify`` makes this
    pass-through (rejection is None) ⇒ the test fails. Complements the
    existing missing-sidecar ``test_modify_rejects_stale_sidecar``."""
    from ops.engine_sdlc.planner import attach_ecr_context, classify, validate
    md = _identity_mismatched_sidecar(tmp_path)
    ecr = _modify_ecr(md)  # engine=reversion, the standard param_change
    vp = validate(attach_ecr_context(
        classify(ecr, {"reversion": LifecycleState.PAPER}), ecr),
        ecr=ecr)
    assert vp.rejection is not None, (
        "an identity-mismatched (different-run) sidecar at the cited "
        "path was wrongly ACCEPTED — BLOCKER 2 unimplemented")
    assert ("identity-stale" in vp.rejection
            or "DIFFERENT Lab run" in vp.rejection), vp.rejection


def test_add_lab_candidate_rejects_identity_mismatched_sidecar(tmp_path):
    """BLOCKER 2 ADD-leg: the SAME identity-freshness gate guards the
    ADD ``lab_candidate`` branch — a valid (promote_new) sidecar from a
    DIFFERENT run at the cited path is a hard reject. NON-VACUOUS: the
    sidecar is otherwise gate-clean; deleting the shared
    ``assert_identity_fresh`` call in validate()'s ADD branch makes this
    pass-through."""
    from ops.engine_sdlc.ecr import EngineChangeRequest
    from ops.engine_sdlc.planner import attach_ecr_context, classify, validate
    from tpcore.lab.models import LabResult
    from tpcore.tests.test_lab_dossier_sidecar import _labresult
    r = _labresult()
    d = r.model_dump()
    d["recommended_exit"] = "promote_new"
    d["intent"] = "promote_new"
    sidecar_lr = LabResult.model_validate(d)  # candidate rev_cand, seed 0
    # cited path names a DIFFERENT run.
    md = tmp_path / "2026-05-18-different_cand-SURVIVED-seed7.md"
    md.write_text("# rendered")
    md.with_suffix(".json").write_text(sidecar_lr.model_dump_json())
    ecr = EngineChangeRequest(
        action="add", engine="newx", source="lab_candidate",
        lab_dossier=str(md), cadence="daily", allocator=False,
        dispatch_order=9, need="x")
    vp = validate(attach_ecr_context(classify(ecr, {}), ecr),
                  repo_root=None, ecr=ecr)
    assert vp.rejection is not None, (
        "an identity-mismatched promote_new sidecar at the cited path "
        "was wrongly ACCEPTED in the ADD lab_candidate branch — "
        "BLOCKER 2 ADD-leg unimplemented")
    assert ("identity-stale" in vp.rejection
            or "DIFFERENT Lab run" in vp.rejection), vp.rejection


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


# ---- T7 hardening (review notes #1/#2): _apply_modify e2e + byte-
# identical multi-file rollback; _validate_modify lifecycle-immutable ----


def test_apply_modify_edits_default_const_and_rolls_back_byte_identical(
        tmp_path):
    """Review note #1 (the real one): drive the REAL apply() entry with a
    clean validated MODIFY ECR all the way through to a multi-file engine
    default-constant edit, then a forced-red byte-identical rollback.

    UNIQUE surface vs the T5-pinned REMOVE rollback (both unpinned before
    this test):
      (a) the by_file MULTI-FILE grouped edit — ONE MODIFY ECR carrying
          z_threshold + max_hold_days hits TWO real source files
          (reversion/models.py:Z_SCORE_THRESHOLD +
          reversion/backtest.py:MAX_HOLD_DAYS — verified against the live
          source / _ENGINE_DEFAULT_CONSTS), the genuine `by_file` group;
      (b) the constant-value compile() AST-gate in _apply_modify (a code
          path distinct from T5's _rewrite_profile_source gate).

    A plain _staged_copytree (NOT _make_synthetic_engine_tree) is used:
    the MODIFY target `reversion` already exists in a clean copytree, and
    a clean MODIFY of parameter CONSTANTS touches no lifecycle/roster, so
    the post-stage consistency subprocess stays GREEN on the success leg
    (the success-path proof is itself non-vacuous — it asserts a None
    rejection AND the changed bytes AND a green subprocess).

    NON-VACUOUS (forced-red leg): the post-stage clockwork is forced red
    by appending a failing test to the staged consistency suite (the T5
    forced-red technique from
    test_apply_red_consistency_rolls_back_to_byte_identical). The
    assertion is the recursive _snapshot_tree byte-oracle over BOTH
    edited files' parent trees: a non-reversing rollback, OR a PARTIAL
    multi-file rollback (file A reverted but file B's edit surviving, or
    vice-versa), changes the map and trips it. Transient-break proof
    (performed during authoring, restored before commit): commenting out
    `jn.restore()` on the rc!=0 branch of apply() makes BOTH the
    edited-file byte assertions AND the recursive-map assertion FAIL
    (extra/missing reported); making restore reverse only the FIRST
    by_file entry (a simulated partial multi-file rollback) leaves the
    second file drifted and the recursive-map assertion FAILs with the
    surviving-edit delta — `git diff` clean after restoring planner.py.
    """
    from ops.engine_sdlc.planner import (
        _staged_copytree,
        apply,
        attach_ecr_context,
        classify,
        validate,
    )
    staged = _staged_copytree(tmp_path / "tree")
    models_py = staged / "reversion" / "models.py"
    backtest_py = staged / "reversion" / "backtest.py"
    profile_py = staged / "tpcore" / "engine_profile.py"

    # REAL pre-edit constant lines (verified against the live source):
    #   reversion/models.py     -> Z_SCORE_THRESHOLD = 2.5
    #   reversion/backtest.py   -> MAX_HOLD_DAYS = 30
    assert "Z_SCORE_THRESHOLD = 2.5" in models_py.read_text()
    assert "MAX_HOLD_DAYS = 30" in backtest_py.read_text()
    before_profile = profile_py.read_bytes()

    # ── Success leg: clean validated MODIFY through the REAL apply() ──
    # fold_existing + SURVIVED + dsr0.97 + cred64 + winning matching the
    # ECR param_change for BOTH keys (the _modify_sidecar/_modify_ecr
    # helpers already default to z_threshold=3.1 + max_hold_days=8, both
    # in reversion PARAM_RANGES). z_threshold lives in models.py,
    # max_hold_days in backtest.py — ONE ECR, the genuine by_file group.
    md = _modify_sidecar(tmp_path)
    ecr = _modify_ecr(md)  # param_change={"z_threshold":"3.1",
    #                                       "max_hold_days":"8"}
    plan = attach_ecr_context(
        classify(ecr, {"reversion": LifecycleState.PAPER}), ecr)
    vplan = validate(plan, ecr=ecr)
    assert vplan.rejection is None, vplan.rejection
    res = apply(vplan, repo_root=staged, emit_audit=False)
    assert res.rejection is None, (
        f"a clean MODIFY through apply() must succeed:\n{res.rejection}")
    # the targeted constants are changed in the CORRECT files to the
    # validated winning values (the by_file grouped multi-file edit):
    new_models = models_py.read_text()
    new_backtest = backtest_py.read_text()
    assert "Z_SCORE_THRESHOLD = 3.1" in new_models, (
        "z_threshold not rewritten in reversion/models.py")
    assert "Z_SCORE_THRESHOLD = 2.5" not in new_models, (
        "stale Z_SCORE_THRESHOLD default survived the MODIFY")
    assert "MAX_HOLD_DAYS = 8" in new_backtest, (
        "max_hold_days not rewritten in reversion/backtest.py")
    assert "MAX_HOLD_DAYS = 30" not in new_backtest, (
        "stale MAX_HOLD_DAYS default survived the MODIFY")
    # AST-gate exercised: the rewritten sources must still parse/compile
    # (apply() returned no rejection => _apply_modify's compile() passed;
    # assert it explicitly so the AST-gate code path is pinned).
    import ast
    ast.parse(new_models)
    ast.parse(new_backtest)
    # H-S3-6d: MODIFY NEVER edits lifecycle — _PROFILE byte-unchanged.
    assert profile_py.read_bytes() == before_profile, (
        "MODIFY mutated tpcore/engine_profile.py — lifecycle is immutable "
        "under MODIFY (H-S3-6d)")
    # the consistency subprocess path was exercised on the SUCCESS leg:
    # a clean param-constant MODIFY leaves the clockwork GREEN (a broken
    # subprocess invocation that always returned non-zero would have
    # produced res.rejection above — this nails it positively).
    from ops.engine_sdlc.planner import _run_consistency_subprocess
    rc, out = _run_consistency_subprocess(staged)
    assert rc == 0, (
        f"a clean param MODIFY must leave the clockwork GREEN:\n{out}")

    # ── Forced-red leg: byte-identical multi-file rollback ──
    # Fresh staged tree so the pre-state is the pristine source.
    staged2 = _staged_copytree(tmp_path / "tree2")
    models2 = staged2 / "reversion" / "models.py"
    backtest2 = staged2 / "reversion" / "backtest.py"
    profile2 = staged2 / "tpcore" / "engine_profile.py"
    tc2 = (staged2 / "tpcore" / "tests"
           / "test_engine_lifecycle_consistency.py")
    # Force the post-stage subprocess red (T5 technique): append a
    # failing test to the staged consistency suite. _apply_modify does
    # NOT touch tc, so tc's pre-apply bytes are exactly this corrupted
    # form — the byte-oracle baseline below captures it AFTER corruption.
    tc2.write_text(tc2.read_text() +
                   "\n\ndef test_forced_red():\n    assert False\n")
    before_files = {p: p.read_bytes()
                    for p in (models2, backtest2, profile2, tc2)}

    def _src_snapshot(root):
        # _snapshot_tree minus subprocess-generated bytecode: the
        # consistency subprocess runs with cwd=staged2 and writes
        # reversion/**/__pycache__/*.pyc that were never in the
        # pre-state. Those are NOT a rollback defect (a surviving
        # CONSTANT edit lives in a .py source file, never a .pyc), so
        # filtering them keeps the partial-multi-file oracle honest
        # while removing pure test-harness noise (same discipline the
        # T6 ADD byte-oracle tests use to scope to the .py surface).
        return {k: v for k, v in _snapshot_tree(root).items()
                if "__pycache__" not in k and not k.endswith(".pyc")}

    before_models_tree = _src_snapshot(staged2 / "reversion")
    s2dir = tmp_path / "s2"
    s2dir.mkdir()
    md2 = _modify_sidecar(s2dir)
    ecr2 = _modify_ecr(md2)
    plan2 = attach_ecr_context(
        classify(ecr2, {"reversion": LifecycleState.PAPER}), ecr2)
    vplan2 = validate(plan2, ecr=ecr2)
    assert vplan2.rejection is None, vplan2.rejection
    res2 = apply(vplan2, repo_root=staged2, emit_audit=False,
                 _force_validate=True)
    assert res2.rejection is not None
    assert "post-stage clockwork red" in res2.rejection, res2.rejection
    # EVERY touched file byte-identical — a partial multi-file rollback
    # (models.py reverted but backtest.py's edit surviving, or vice
    # versa) trips one of these directly:
    for p, b in before_files.items():
        assert p.read_bytes() == b, (
            f"{p.relative_to(staged2)} not restored byte-identical after "
            f"forced-red rollback (partial/no multi-file rollback)")
    # recursive byte-oracle over the WHOLE reversion/ package: NO drifted
    # constant survives in EITHER file (closes the partial-multi-file
    # window — if file A's edit was reverted but file B's was not, the
    # map differs and reports the surviving-edit delta):
    after_models_tree = _src_snapshot(staged2 / "reversion")
    assert after_models_tree == before_models_tree, (
        "forced-red MODIFY did not restore reversion/ byte-identical — "
        f"extra={set(after_models_tree) - set(before_models_tree)} "
        f"missing={set(before_models_tree) - set(after_models_tree)}")
    # the pristine defaults are back in BOTH files (explicit, in case a
    # future refactor changes _snapshot_tree's root):
    assert "Z_SCORE_THRESHOLD = 2.5" in models2.read_text(), (
        "models.py default not restored on forced-red rollback")
    assert "MAX_HOLD_DAYS = 30" in backtest2.read_text(), (
        "backtest.py default not restored on forced-red rollback")
    # loud escalation surfaced (not swallowed into a false-green):
    assert res2.rejection and "rc=" in res2.rejection


def test_validate_modify_rejects_lifecycle_key_in_sot_diff():
    """Review note #2 (cheap defense-in-depth): _validate_modify's
    sot_diff lifecycle-immutable guard (planner.py ~598-604) must LOUDLY
    reject a MODIFY plan whose sot_diff carries ANY lifecycle key
    (lifecycle_state / allocator_eligible / dispatch_order / cadence) —
    lifecycle is immutable under MODIFY (H-S3-6d).

    The normal path (classify -> attach_ecr_context for MODIFY) only
    threads lab_dossier/param_change/gate_* into sot_diff, so a lifecycle
    key cannot arrive organically — per the task, directly construct the
    TransitionPlan with such a sot_diff and pass it to _validate_modify
    (the real guarded function, exported in planner.__all__).

    NON-VACUOUS: each forbidden key is asserted independently, the
    rejection substring is pinned ("lifecycle is immutable"), and a clean
    sot_diff (no lifecycle key) is asserted to PASS — so deleting the
    guard (planner.py ~598-604) makes the four reject-asserts fail while
    the clean-pass control proves the gate is not a constant reject.
    """
    import tempfile

    from ops.engine_sdlc.ecr import ECRAction
    from ops.engine_sdlc.planner import (
        ApprovalClass,
        TransitionPlan,
        _validate_modify,
    )
    # a sidecar+ECR that, by itself, fully PASSES the zero-trust gate so
    # the ONLY thing under test is the lifecycle-key guard.
    with tempfile.TemporaryDirectory() as td:
        from pathlib import Path as _P
        tdp = _P(td)
        md = _modify_sidecar(tdp)
        ecr = _modify_ecr(md)
        base_kw = dict(
            action=ECRAction.MODIFY, engine="reversion",
            from_state=LifecycleState.PAPER, to_state=LifecycleState.PAPER,
            approval_class=ApprovalClass.AUTOMATED,
            gate_checks=["modify_evidence"])
        # control: a sot_diff with ONLY the legit MODIFY context keys
        # PASSES (proves the guard is not a blanket reject).
        clean = TransitionPlan(**base_kw, sot_diff={
            "lab_dossier": str(md),
            "param_change": {"z_threshold": "3.1", "max_hold_days": "8"},
            "gate_dsr": 0.97, "gate_cred": 64})
        ok = _validate_modify(clean, ecr)
        assert ok.rejection is None, (
            f"clean MODIFY sot_diff must pass the guard: {ok.rejection}")
        # each forbidden lifecycle key, injected into sot_diff alongside
        # the otherwise-valid context, must be LOUDLY rejected:
        for bad_key, bad_val in (
                ("lifecycle_state", "PAPER"),
                ("allocator_eligible", True),
                ("dispatch_order", 9),
                ("cadence", "daily")):
            tampered = TransitionPlan(**base_kw, sot_diff={
                "lab_dossier": str(md),
                "param_change": {"z_threshold": "3.1",
                                 "max_hold_days": "8"},
                "gate_dsr": 0.97, "gate_cred": 64,
                bad_key: bad_val})
            rej = _validate_modify(tampered, ecr)
            assert rej.rejection is not None, (
                f"MODIFY plan carrying lifecycle key {bad_key!r} was NOT "
                f"rejected — the H-S3-6d guard is missing/broken")
            assert "lifecycle is immutable" in rej.rejection, (
                f"reject for {bad_key!r} lacks the pinned H-S3-6d "
                f"substring: {rej.rejection!r}")
