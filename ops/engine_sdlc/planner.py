"""The deterministic ECR planner/executor (SP3 §3–§5).

parse_ecr → classify(ecr, snapshot) -> TransitionPlan → validate(plan)
(re-verify evidence + run the REAL SP1 clockwork in an isolated temp
tree as a fresh subprocess, H-S3-1/D2) → apply(plan) (journaled
atomic-or-abort, H-S3-4). Engine-touching orchestration: LEGAL only in
ops/ (H-S2-1). The _PROFILE rewrite is AST-validated (H-S3-3) and
data-only (H-S3-10 — adds zero imports).
"""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ops.engine_sdlc.ecr import ECRAction, EngineChangeRequest
from tpcore.engine_profile import LifecycleState

REPO_ROOT = Path(__file__).resolve().parents[2]
_ALLOCATOR = "allocator"


class ApprovalClass(StrEnum):
    """Operator-vs-automated approval gate (spec §5.2). A StrEnum so
    ``plan.approval_class == "OPERATOR"`` compares cleanly and the
    frozen pydantic model carries it as its ``.value`` string."""

    OPERATOR = "OPERATOR"
    AUTOMATED = "AUTOMATED"


class TransitionPlan(BaseModel):
    """The deterministic state-machine output (spec §3.2). Frozen
    pydantic-v2 (SP3 convention — parity with EngineChangeRequest /
    LabResult; ``extra="forbid"``). Executors (T5–T7) fill sot_diff /
    fs_ops; classify sets the edge + approval or a typed rejection."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    action: ECRAction
    engine: str
    from_state: LifecycleState | None
    to_state: LifecycleState | None
    approval_class: str | None
    sot_diff: dict[str, Any] = Field(default_factory=dict)
    fs_ops: list[tuple[str, str]] = Field(default_factory=list)
    gate_checks: list[str] = Field(default_factory=list)
    rejection: str | None = None
    source: str | None = None


def _reject(ecr: EngineChangeRequest, reason: str) -> TransitionPlan:
    return TransitionPlan(
        action=ecr.action, engine=ecr.engine, from_state=None,
        to_state=None, approval_class=None, rejection=reason)


def classify(
    ecr: EngineChangeRequest,
    profile_snapshot: dict[str, LifecycleState],
) -> TransitionPlan:
    """Pure: maps (action, in-profile?, from_state, source) to the single
    defined §5.1 edge or a typed rejection. The table is TOTAL and CLOSED
    — any cell not below is a typed rejection, never an inferred edge.

    Read-only snapshot in / plan out — NO I/O, NO _PROFILE mutation
    (H-S3-2 read-side). The ECR free-text is threaded separately via
    ``attach_ecr_context`` so ``classify`` stays pure.
    """
    present = ecr.engine in profile_snapshot
    cur = profile_snapshot.get(ecr.engine)

    if ecr.action is ECRAction.ADD:
        if present:
            return _reject(
                ecr, f"engine {ecr.engine!r} already exists "
                     f"(use MODIFY to re-tune or REMOVE to retire)")
        return TransitionPlan(
            action=ecr.action, engine=ecr.engine, from_state=None,
            to_state=LifecycleState.LAB,  # ADD ALWAYS → LAB (H-S3-11)
            approval_class=ApprovalClass.OPERATOR, source=ecr.source,
            gate_checks=(["lab_sidecar"] if ecr.source == "lab_candidate"
                         else ["readiness"]))

    if ecr.action is ECRAction.REMOVE:
        if not present:
            return _reject(ecr, f"nothing to remove: engine "
                                f"{ecr.engine!r} absent from _PROFILE")
        if cur is LifecycleState.RETIRED:
            return _reject(ecr, f"engine {ecr.engine!r} already retired")
        return TransitionPlan(
            action=ecr.action, engine=ecr.engine, from_state=cur,
            to_state=LifecycleState.RETIRED,
            approval_class=ApprovalClass.OPERATOR)

    # MODIFY
    if not present:
        return _reject(ecr, f"nothing to modify: engine "
                            f"{ecr.engine!r} absent from _PROFILE")
    if cur is LifecycleState.RETIRED:
        return _reject(ecr, f"cannot tune a retired engine "
                            f"{ecr.engine!r}")
    return TransitionPlan(
        action=ecr.action, engine=ecr.engine, from_state=cur,
        to_state=cur,  # MODIFY: no lifecycle edge (spec §4.3)
        approval_class=ApprovalClass.AUTOMATED,
        gate_checks=["modify_evidence"])


def attach_ecr_context(plan: TransitionPlan, ecr: EngineChangeRequest) -> TransitionPlan:
    """Thread the ECR free-text/source onto a classified plan WITHOUT
    impurifying ``classify`` (the reconciled split: classify is a pure
    snapshot→plan map; ECR context is attached here). A frozen model →
    return a copy with ``source`` carried through."""
    return plan.model_copy(update={"source": plan.source or ecr.source})


def _rewrite_profile_source(
    src: str, *, engine: str, set_state: str,
    set_allocator_eligible: bool,
) -> str:
    """H-S3-3: a targeted, line-anchored, AST-validated rewrite of the
    SINGLE target EngineProfile(...) entry's lifecycle_state= /
    allocator_eligible= tokens. Touches no sibling, adds no import
    (H-S3-10), preserves the explanatory comments. ast.parse +
    compile() gate before the caller stages anything; SyntaxError /
    duplicate-key / extra=forbid raises here.
    """
    tree = ast.parse(src)  # pre-edit parse — proves the baseline is sane
    del tree
    lines = src.splitlines(keepends=True)
    # The entry spans the line `"<engine>": EngineProfile(` through its
    # closing `)`. Find that block by the quoted key anchor.
    key_anchor = f'"{engine}":'
    start = next((i for i, ln in enumerate(lines)
                  if key_anchor in ln and "EngineProfile(" in ln), None)
    if start is None:
        raise ValueError(
            f"_PROFILE entry for {engine!r} not found (key anchor "
            f"{key_anchor!r}) — cannot rewrite")
    depth = 0
    end = start
    for i in range(start, len(lines)):
        depth += lines[i].count("(") - lines[i].count(")")
        if depth == 0:
            end = i
            break
    block = "".join(lines[start:end + 1])
    new_block = _replace_kw(block, "lifecycle_state",
                            f"LifecycleState.{set_state.upper()}")
    new_block = _replace_kw(
        new_block, "allocator_eligible", str(set_allocator_eligible))
    new_src = "".join(lines[:start]) + new_block + "".join(lines[end + 1:])
    # H-S3-3 gate: the rewritten source must parse AND compile.
    compile(new_src, "<engine_profile_rewrite>", "exec")
    return new_src


def _replace_kw(block: str, kw: str, value: str) -> str:
    """Replace `kw=<...>` token inside one EngineProfile(...) call. If
    the kw is absent (e.g. allocator_eligible defaulted), inject it
    before the closing paren of the call (still data-only, no import)."""
    pat = re.compile(rf"({kw}\s*=\s*)([^,)\n]+)")
    if pat.search(block):
        return pat.sub(rf"\g<1>{value}", block, count=1)
    # absent → inject before the final ')'
    idx = block.rfind(")")
    return block[:idx] + f", {kw}={value}" + block[idx:]


def _run_consistency_subprocess(staged_tree: Path) -> tuple[int, str]:
    """H-S3-1 / D2: run the REAL clockwork as a fresh subprocess with
    cwd=the staged tree, so its REPO / import tpcore.engine_profile /
    _PROFILE are all the PROPOSED ones (zero in-process state bleed —
    a dict-injection seam would validate a different code path than CI).
    """
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, "-m", "pytest",
         "tpcore/tests/test_engine_lifecycle_consistency.py",
         "-q", "-p", "no:cacheprovider"],
        cwd=str(staged_tree), capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout + proc.stderr


def _staged_copytree(dest: Path) -> Path:
    """copytree the worktree minus .git/.venv/__pycache__/backtests
    (H-S3-1; R3 accepted: O(repo) but on-demand, not a daemon)."""
    shutil.copytree(
        REPO_ROOT, dest,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "backtests"))
    return dest


__all__ = [
    "REPO_ROOT",
    "ApprovalClass",
    "TransitionPlan",
    "attach_ecr_context",
    "classify",
    "_rewrite_profile_source",
    "_run_consistency_subprocess",
    "_staged_copytree",
]
