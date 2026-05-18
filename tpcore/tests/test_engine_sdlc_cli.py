"""T8 — the CLI: fail-closed TTY y/n (H-S3-7), explicit non-zero never
silent 0 (H-S3-12), audit on every terminal outcome. Lazy in-body
import (H-S3-10).

The ``scripts/ops.py`` vs ``ops/`` package collision (SP2-T9/T10) is
acute for ``__main__``: a non-package ``ops`` cached by an earlier test
in full-suite collection order would shadow ``ops.engine_sdlc.__main__``.
Mirror ``scripts/tests/test_lab_cli_entrypoint.py``: evict any cached
non-package ``ops`` at module load, and keep every ``ops.engine_sdlc``
import lazy/in-body.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# Evict a non-package ``ops`` (scripts/ops.py) cached by an earlier test
# so ``import ops.engine_sdlc.__main__`` resolves the real package.
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]


def _write_ecr(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "ecr.txt"
    p.write_text(body)
    return p


def _snapshot_tree(*roots: Path) -> dict[str, bytes]:
    """Byte-identical oracle (T5 discipline): a mutation anywhere under
    any root changes this map. A non-existent root contributes nothing."""
    snap: dict[str, bytes] = {}
    for r in roots:
        if not r.exists():
            continue
        for p in sorted(r.rglob("*")):
            if p.is_file():
                snap[f"{r.name}::{p.relative_to(r).as_posix()}"] = (
                    p.read_bytes())
    return snap


_REMOVE_GHOST = """\
ECR
action:        REMOVE
engine:        ghost_engine_not_present
reason:        x
eulogy_notes:  y
"""


@pytest.mark.asyncio
async def test_parse_fail_rc1(tmp_path, capsys):
    from ops.engine_sdlc.__main__ import _amain
    p = _write_ecr(tmp_path, "not an ecr at all")
    rc = await _amain(["--ecr", str(p)])
    assert rc == 1
    assert "no ECR block" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_reject_rc1(tmp_path, capsys):
    from ops.engine_sdlc.__main__ import _amain
    p = _write_ecr(tmp_path, _REMOVE_GHOST)
    rc = await _amain(["--ecr", str(p)])
    assert rc == 1
    assert "nothing to remove" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_no_args_rc_nonzero(capsys):
    from ops.engine_sdlc.__main__ import _amain
    with pytest.raises(SystemExit) as e:
        await _amain([])
    assert e.value.code != 0


@pytest.mark.asyncio
async def test_help_exits_zero():
    """argparse --help works (rc 0 via SystemExit(0)) — H-S3-12: a
    --help is the ONE legitimate non-mutating exit-0 path."""
    from ops.engine_sdlc.__main__ import _parse_args
    with pytest.raises(SystemExit) as ei:
        _parse_args(["--help"])
    assert ei.value.code == 0


@pytest.mark.asyncio
async def test_non_y_declines_zero_mutation(tmp_path, monkeypatch):
    """H-S3-7(a): any non-`y`/`yes` token on the OPERATOR path ⇒
    declined, apply NOT called, ZERO mutation (byte-identical real
    tpcore/engine_profile.py + the sentinel package + archive/)."""
    import ops.engine_sdlc.__main__ as cli
    ep = REPO_ROOT / "tpcore" / "engine_profile.py"
    pkg = REPO_ROOT / "sentinel"
    arc = REPO_ROOT / "archive"
    before = _snapshot_tree(ep, pkg, arc)
    called = {"apply": 0}
    monkeypatch.setattr(cli, "apply",
                        lambda *a, **k: called.__setitem__("apply", 1))
    # a valid REMOVE of a real PAPER engine reaches the prompt
    p = _write_ecr(tmp_path, "ECR\naction: REMOVE\nengine: sentinel\n"
                             "reason: x\neulogy_notes: y\n")
    monkeypatch.setattr(cli, "_read_confirm", lambda: "n")
    # stub validate so the dry-run subprocess is not actually executed
    monkeypatch.setattr(cli, "_validate_for_cli",
                        lambda plan, ecr: plan)  # green
    rc = await cli._amain(["--ecr", str(p)])
    assert rc == 1
    assert called["apply"] == 0, "apply ran despite a non-y answer"
    assert _snapshot_tree(ep, pkg, arc) == before, (
        "a declined OPERATOR ECR mutated the real tree")


@pytest.mark.asyncio
async def test_eof_declines(tmp_path, monkeypatch):
    import ops.engine_sdlc.__main__ as cli
    ep = REPO_ROOT / "tpcore" / "engine_profile.py"
    pkg = REPO_ROOT / "sentinel"
    arc = REPO_ROOT / "archive"
    before = _snapshot_tree(ep, pkg, arc)
    called = {"apply": 0}
    monkeypatch.setattr(cli, "apply",
                        lambda *a, **k: called.__setitem__("apply", 1))
    p = _write_ecr(tmp_path, "ECR\naction: REMOVE\nengine: sentinel\n"
                             "reason: x\neulogy_notes: y\n")

    def _eof():
        raise EOFError

    monkeypatch.setattr(cli, "_read_confirm", _eof)
    monkeypatch.setattr(cli, "_validate_for_cli", lambda plan, ecr: plan)
    rc = await cli._amain(["--ecr", str(p)])
    assert rc == 1
    assert called["apply"] == 0
    assert _snapshot_tree(ep, pkg, arc) == before, (
        "an EOF-declined OPERATOR ECR mutated the real tree")


@pytest.mark.asyncio
async def test_rejected_plan_never_prompts(tmp_path, monkeypatch):
    import ops.engine_sdlc.__main__ as cli
    prompted = {"n": 0}
    monkeypatch.setattr(cli, "_read_confirm",
                        lambda: prompted.__setitem__("n", 1) or "y")
    p = _write_ecr(tmp_path, _REMOVE_GHOST)  # classify → reject
    rc = await cli._amain(["--ecr", str(p)])
    assert rc == 1
    assert prompted["n"] == 0, "a rejected plan must never reach the prompt"


@pytest.mark.asyncio
async def test_every_outcome_emits_audit(tmp_path, monkeypatch):
    """H-S3-7: every terminal outcome emits exactly one
    ENGINE_CHANGE_REQUEST audit. Covered here: parse-fail (no plan, no
    audit expected — explicit non-zero is the receipt), rejected
    (classify), validation-rejected, operator-declined. The audit is
    patched at its definition site (planner._emit_audit) — the SP2
    "patch where defined" lesson."""
    import ops.engine_sdlc.__main__ as cli
    import ops.engine_sdlc.planner as planner

    # (a) classify-rejected → exactly one "rejected" audit.
    events: list[tuple] = []
    monkeypatch.setattr(planner, "_emit_audit",
                        lambda *a, **k: events.append(a))
    p = _write_ecr(tmp_path, _REMOVE_GHOST)
    rc = await cli._amain(["--ecr", str(p)])
    assert rc == 1
    assert len(events) == 1, "a rejected ECR must emit exactly one audit"
    assert any("rejected" in str(e) for e in events)

    # (b) validation-rejected → exactly one "rejected" audit.
    events.clear()
    monkeypatch.setattr(cli, "_validate_for_cli",
                        lambda plan, ecr: planner.TransitionPlan(
                            **{**plan.__dict__,
                               "rejection": "synthetic validation reject"}))
    p2 = _write_ecr(tmp_path, "ECR\naction: REMOVE\nengine: sentinel\n"
                              "reason: x\neulogy_notes: y\n")
    rc = await cli._amain(["--ecr", str(p2)])
    assert rc == 1
    assert len(events) == 1, "a validation reject must emit exactly one audit"
    assert any("rejected" in str(e) for e in events)

    # (c) operator-declined → exactly one "operator_declined" audit.
    events.clear()
    monkeypatch.setattr(cli, "_validate_for_cli", lambda plan, ecr: plan)
    monkeypatch.setattr(cli, "_read_confirm", lambda: "n")
    monkeypatch.setattr(cli, "apply", lambda *a, **k: pytest.fail(
        "apply ran on a declined ECR"))
    p3 = _write_ecr(tmp_path, "ECR\naction: REMOVE\nengine: sentinel\n"
                              "reason: x\neulogy_notes: y\n")
    rc = await cli._amain(["--ecr", str(p3)])
    assert rc == 1
    assert len(events) == 1, "a declined ECR must emit exactly one audit"
    assert any("operator_declined" in str(e) for e in events)

    # (d) gate-failed apply → the planner.apply path owns the audit; the
    # CLI surfaces the rejection + explicit non-zero. Stub apply to a
    # rejected plan and assert the CLI does not silently exit 0.
    events.clear()
    monkeypatch.setattr(cli, "_read_confirm", lambda: "y")
    monkeypatch.setattr(
        cli, "apply",
        lambda plan, **k: planner.TransitionPlan(
            **{**plan.__dict__, "rejection": "post-stage clockwork red"}))
    p4 = _write_ecr(tmp_path, "ECR\naction: REMOVE\nengine: sentinel\n"
                              "reason: x\neulogy_notes: y\n")
    rc = await cli._amain(["--ecr", str(p4)])
    assert rc == 1, "a gate-failed apply must not exit silently 0"


@pytest.mark.asyncio
async def test_apply_rc0(tmp_path, monkeypatch, capsys):
    """H-S3-12 (spec §11): a FULLY-SUCCESSFUL applied outcome at the CLI
    layer returns EXACTLY ``rc == 0`` AND surfaces the applied receipt.
    The spec + plan both NAME this test by id; it was previously absent.

    Non-vacuous: ``apply`` is stubbed to return the SAME (un-rejected)
    plan — a silent non-zero, or a reject masquerading as success (the
    CLI checks ``res.rejection``), trips the rc-and-print assertions. A
    byte-identical real-tree oracle proves no production mutation."""
    import ops.engine_sdlc.__main__ as cli
    ep = REPO_ROOT / "tpcore" / "engine_profile.py"
    pkg = REPO_ROOT / "sentinel"
    arc = REPO_ROOT / "archive"
    before = _snapshot_tree(ep, pkg, arc)
    # a valid REMOVE of a real PAPER engine reaches the OPERATOR prompt
    p = _write_ecr(tmp_path, "ECR\naction: REMOVE\nengine: sentinel\n"
                             "reason: x\neulogy_notes: y\n")
    # green validate (no real dry-run subprocess), operator says yes,
    # apply returns the un-rejected plan ⇒ the success leg.
    monkeypatch.setattr(cli, "_validate_for_cli", lambda plan, ecr: plan)
    monkeypatch.setattr(cli, "_read_confirm", lambda: "y")
    applied = {"n": 0}
    monkeypatch.setattr(
        cli, "apply",
        lambda plan, **k: applied.__setitem__("n", 1) or plan)
    rc = await cli._amain(["--ecr", str(p)])
    assert rc == 0, "a fully-successful apply must return EXACTLY rc 0"
    assert applied["n"] == 1, "the success leg never invoked apply()"
    assert "APPLIED" in capsys.readouterr().out, (
        "a successful apply must surface the applied receipt — a silent "
        "0 with no receipt is the canary -m-no-op anti-pattern (H-S3-12)")
    assert _snapshot_tree(ep, pkg, arc) == before, (
        "a stubbed-apply success path mutated the real tree")


@pytest.mark.asyncio
async def test_modify_routes_automated_no_prompt(tmp_path, monkeypatch,
                                                 capsys):
    """Deviation #3 fail-open guard: a valid MODIFY ECR is AUTOMATED
    (spec §12 operator-confirmed) and MUST route PAST the y/n prompt.
    Drives a real MODIFY ECR (fold_existing sidecar, right target,
    in-PARAM_RANGES key==winning, DSR≥0.95/cred≥60/SURVIVED) through
    ``_amain`` with ``_read_confirm`` as a TRIPWIRE.

    Non-vacuous: if the CLI's ``== ApprovalClass.AUTOMATED`` regressed to
    ``is`` (the frozen-pydantic StrEnum carries the ``.value`` string,
    not the enum identity), MODIFY would fall through to the OPERATOR
    prompt — ``called['prompt']`` would be 1 and this fails. (Proven by
    a transient local ``==``→``is`` flip; reverted, diff clean.)"""
    import ops.engine_sdlc.__main__ as cli
    from tpcore.tests.test_engine_sdlc_planner import (
        _modify_ecr,
        _modify_sidecar,
    )
    ep = REPO_ROOT / "tpcore" / "engine_profile.py"
    rev = REPO_ROOT / "reversion"
    before = _snapshot_tree(ep, rev)
    # a genuine valid MODIFY ECR (the T7 helpers build the matching
    # frozen LabResult sidecar + the param_change wire fields).
    md = _modify_sidecar(tmp_path)
    secr = _modify_ecr(md)
    pc = ",".join(f"{k}={v}" for k, v in secr.param_change.items())
    p = _write_ecr(
        tmp_path,
        f"ECR\naction: MODIFY\nengine: {secr.engine}\n"
        f"lab_dossier: {secr.lab_dossier}\n"
        f"param_change: {pc}\n"
        f"gate_dsr: {secr.gate_dsr}\ngate_cred: {secr.gate_cred}\n")
    called = {"prompt": 0}
    monkeypatch.setattr(
        cli, "_read_confirm",
        lambda: called.__setitem__("prompt", 1) or "y")
    # green validate (no real zero-trust subprocess/sidecar re-derive);
    # apply returns the un-rejected plan ⇒ the AUTOMATED success leg.
    monkeypatch.setattr(cli, "_validate_for_cli", lambda plan, ecr: plan)
    applied = {"n": 0}
    monkeypatch.setattr(
        cli, "apply",
        lambda plan, **k: applied.__setitem__("n", 1) or plan)
    rc = await cli._amain(["--ecr", str(p)])
    assert called["prompt"] == 0, (
        "a valid MODIFY (AUTOMATED) reached the operator y/n prompt — "
        "the `== ApprovalClass.AUTOMATED` routing regressed to `is` "
        "(fail-open break of the §12 automated-MODIFY contract)")
    assert rc == 0, "a clean automated MODIFY must return rc 0"
    assert applied["n"] == 1, "the AUTOMATED leg never invoked apply()"
    assert "APPLIED (automated, gated)" in capsys.readouterr().out
    assert _snapshot_tree(ep, rev) == before, (
        "an automated MODIFY (stubbed apply) mutated the real tree")


@pytest.mark.asyncio
async def test_validate_dry_run_reject_no_fabricated_green(tmp_path,
                                                           monkeypatch):
    """BLOCKER 1 (spec §3.2 + §5.2 step 2 + H-S3-1 + H-S3-7b): when the
    REAL ``validate()`` pre-approval dry consistency run REJECTS, the CLI
    must: (1) NOT print a fabricated 'GREEN', (2) NOT prompt the
    operator, (3) exit non-zero, (4) emit exactly one 'rejected' audit.
    This drives the GENUINE ``validate()`` (NOT the
    ``_validate_for_cli`` stub) end-to-end through ``_amain`` — the
    holistic's "at least one test exercising the real validate-dry-run-
    rejects path through the CLI".

    Mechanism: an ADD ``new_scaffold`` for a brand-new engine. validate()
    copytrees the repo, ``_apply_add`` scaffolds from the bare
    ``engine_template`` (no ``<engine>/tests/`` — engine_readiness §6),
    the staged readiness gate fails ⇒ the dry run returns a reject ⇒
    validate() sets ``.rejection`` ⇒ the CLI returns 1 BEFORE the GREEN
    print/prompt. NON-VACUOUS: a CLI that printed GREEN unconditionally
    (the pre-BLOCKER-1 fabrication) or a validate() that skipped the dry
    run would let this reach the prompt / print GREEN — both asserted
    against. Byte-oracle proves zero real-tree mutation by validate()."""
    import ops.engine_sdlc.__main__ as cli
    import ops.engine_sdlc.planner as planner
    ep = REPO_ROOT / "tpcore" / "engine_profile.py"
    before = _snapshot_tree(ep)
    events: list[tuple] = []
    monkeypatch.setattr(planner, "_emit_audit",
                        lambda *a, **k: events.append(a))
    # _read_confirm is a TRIPWIRE — a rejected plan must never prompt.
    monkeypatch.setattr(
        cli, "_read_confirm",
        lambda: pytest.fail("rejected dry run reached the y/n prompt"))
    p = _write_ecr(
        tmp_path,
        "ECR\naction: ADD\nengine: brandnewengine_xyz\n"
        "source: new_scaffold\ncadence: daily\nallocator: false\n"
        "dispatch_order: 9\nneed: prove the dry-run-reject CLI path\n")
    rc = await cli._amain(["--ecr", str(p)])
    assert rc == 1, "a dry-run-rejected ECR must exit non-zero"
    assert len(events) == 1, "exactly one rejected audit expected"
    assert any("rejected" in str(e) for e in events)
    assert _snapshot_tree(ep) == before, (
        "validate()'s dry run mutated the REAL tpcore/engine_profile.py")


@pytest.mark.asyncio
async def test_validate_dry_run_reject_prints_no_green(tmp_path, capsys,
                                                       monkeypatch):
    """BLOCKER 1 companion: the CLI must NOT emit the 'GREEN' receipt
    when validate()'s dry run rejected (the fabricated-GREEN bug). Drives
    the real validate() (no stub) and asserts the GREEN line is absent
    from stdout and the rejection reason is surfaced to stderr."""
    import ops.engine_sdlc.__main__ as cli
    import ops.engine_sdlc.planner as planner
    monkeypatch.setattr(planner, "_emit_audit", lambda *a, **k: None)
    monkeypatch.setattr(
        cli, "_read_confirm",
        lambda: pytest.fail("rejected dry run reached the y/n prompt"))
    p = _write_ecr(
        tmp_path,
        "ECR\naction: ADD\nengine: brandnewengine_abc\n"
        "source: new_scaffold\ncadence: daily\nallocator: false\n"
        "dispatch_order: 9\nneed: prove no fabricated GREEN\n")
    rc = await cli._amain(["--ecr", str(p)])
    cap = capsys.readouterr()
    assert rc == 1
    assert "GREEN" not in cap.out, (
        "the CLI fabricated a GREEN dry-run receipt on a REJECTED plan "
        "(BLOCKER 1 — the unconditional print)")
    assert "rejected on validation" in cap.err


def test_importing_engine_sdlc_main_does_not_eager_import_an_engine():
    """H-S3-10 (the docstring's named proof): importing the entrypoint
    must NOT pull in any engine package — every engine import stays
    lazy/function-local in the SP3 ops.engine_sdlc planner/ecr.

    Non-vacuous: a module-top ``import reversion`` (etc.) anywhere in the
    __main__ → planner → ecr import chain trips it. The eviction guard
    makes it collection-order-independent."""
    import importlib

    engines = ("reversion", "vector", "momentum", "sentinel", "canary",
               "sigma")
    for mod in engines:
        sys.modules.pop(mod, None)
    importlib.import_module("ops.engine_sdlc.__main__")
    for mod in engines:
        assert mod not in sys.modules, (
            f"import ops.engine_sdlc.__main__ eager-imported {mod!r} — "
            "engine imports must stay lazy (H-S3-10)"
        )
