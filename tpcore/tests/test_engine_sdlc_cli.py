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
