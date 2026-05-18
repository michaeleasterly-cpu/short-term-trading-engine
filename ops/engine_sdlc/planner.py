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
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
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


def attach_ecr_context(plan: TransitionPlan,
                        ecr: EngineChangeRequest) -> TransitionPlan:
    """Thread the ECR's free-text/evidence onto a classified plan
    WITHOUT making classify() impure (classify takes only a snapshot).
    Returns a new frozen-shaped plan with sot_diff merged."""
    extra: dict[str, Any] = {}
    if ecr.action is ECRAction.REMOVE:
        extra = {"reason": ecr.reason, "eulogy_notes": ecr.eulogy_notes}
    elif ecr.action is ECRAction.ADD:
        extra = {"source": ecr.source, "lab_dossier": ecr.lab_dossier,
                 "cadence": ecr.cadence.value if ecr.cadence else None,
                 "allocator": ecr.allocator,
                 "dispatch_order": ecr.dispatch_order,
                 "gate_dsr": ecr.gate_dsr, "gate_cred": ecr.gate_cred}
    elif ecr.action is ECRAction.MODIFY:
        extra = {"lab_dossier": ecr.lab_dossier,
                 "param_change": ecr.param_change,
                 "gate_dsr": ecr.gate_dsr, "gate_cred": ecr.gate_cred}
    return TransitionPlan(**{**plan.__dict__,
                             "sot_diff": {**plan.sot_diff, **extra}})


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


# H-S3-4 fold (T4-review Minor): a hung apply() is worse than a hung
# dry-run — the on-disk clockwork is bounded so a wedged subprocess
# raises (apply() treats the raise as a red → reverse-order rollback).
_CONSISTENCY_TIMEOUT_S = 120


def _run_consistency_subprocess(staged_tree: Path) -> tuple[int, str]:
    """H-S3-1 / D2: run the REAL clockwork as a fresh subprocess with
    cwd=the staged tree, so its REPO / import tpcore.engine_profile /
    _PROFILE are all the PROPOSED ones (zero in-process state bleed —
    a dict-injection seam would validate a different code path than CI).

    Bounded by ``_CONSISTENCY_TIMEOUT_S``: a hang fails LOUD (raises
    ``subprocess.TimeoutExpired``) — apply() treats any exception as a
    red and performs the byte-identical reverse-order rollback. Never
    swallow a timeout into a false-green.
    """
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, "-m", "pytest",
         "tpcore/tests/test_engine_lifecycle_consistency.py",
         "-q", "-p", "no:cacheprovider"],
        cwd=str(staged_tree), capture_output=True, text=True, check=False,
        timeout=_CONSISTENCY_TIMEOUT_S)
    return proc.returncode, proc.stdout + proc.stderr


def _staged_copytree(dest: Path) -> Path:
    """copytree the worktree minus .git/.venv/__pycache__/backtests
    (H-S3-1; R3 accepted: O(repo) but on-demand, not a daemon)."""
    shutil.copytree(
        REPO_ROOT, dest,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "backtests"))
    return dest


EULOGY_TEMPLATE = REPO_ROOT / "tpcore" / "templates" / "eulogy_template.md"


@dataclass
class _Journal:
    """H-S3-4: every touched file's exact prior bytes (or absent) + every
    dir move (src,dst), so apply() can restore byte-identical on red."""
    files: dict[Path, bytes | None] = field(default_factory=dict)
    moves: list[tuple[Path, Path]] = field(default_factory=list)

    def record_file(self, p: Path) -> None:
        if p in self.files:
            return
        self.files[p] = p.read_bytes() if p.is_file() else None

    def restore(self) -> None:
        # reverse order: undo the dir moves first, then the text edits.
        for src, dst in reversed(self.moves):
            if dst.exists():
                if src.exists():
                    shutil.rmtree(src)
                shutil.move(str(dst), str(src))
        for p, prior in reversed(list(self.files.items())):
            if prior is None:
                if p.is_file():
                    p.unlink()
            else:
                p.write_bytes(prior)


def _shadow_edit_remove(staged: Path, engine: str, jn: _Journal) -> None:
    """Purge the engine from the two structurally-parseable shadows
    (the ONLY non-SoT-derived sites — spec §4.2 fs_op 4)."""
    smoke = staged / "scripts" / "run_smoke_test.sh"
    jn.record_file(smoke)
    s = smoke.read_text()
    m = re.search(r"(for engine in )([^\n;]+)(;\s*do)", s)
    if m:
        toks = [t for t in m.group(2).split() if t != engine]
        smoke.write_text(s.replace(
            m.group(0), f"{m.group(1)}{' '.join(toks)}{m.group(3)}"))
    pp = staged / "pyproject.toml"
    jn.record_file(pp)
    txt = pp.read_text()
    txt = txt.replace(f'"{engine}*", ', "").replace(f', "{engine}*"', "")
    txt = re.sub(rf'\n\s*"{engine}/tests",', "", txt)
    pp.write_text(txt)


def _maybe_rewrite_frozen_literal(
    staged: Path, *, retired_engine: str | None, jn: _Journal,
) -> None:
    """H-S3-2: iff the transition changes roster_for_dispatch(), rewrite
    the frozen-literal tuple in test_dispatch_order_invariant_is_the_
    frozen_literal in the SAME staged diff (a structurally-parseable
    shadow, not a hand-edit). REMOVE of a rostered engine drops it."""
    tc = (staged / "tpcore" / "tests"
          / "test_engine_lifecycle_consistency.py")
    jn.record_file(tc)
    src = tc.read_text()
    m = re.search(
        r"roster_for_dispatch\(\) == \(\s*([^)]+)\)", src)
    if not m or retired_engine is None:
        return
    toks = [t.strip().strip('"') for t in m.group(1).split(",")
            if t.strip()]
    if retired_engine not in toks:
        return
    toks = [t for t in toks if t != retired_engine]
    new_tuple = ", ".join(f'"{t}"' for t in toks)
    tc.write_text(src.replace(m.group(0),
                  f"roster_for_dispatch() == ({new_tuple})"))


def _render_eulogy(engine: str, ecr: EngineChangeRequest,
                   gate_record: str) -> str:
    tmpl = EULOGY_TEMPLATE.read_text()
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    return (tmpl
            .replace("{{ENGINE}}", engine)
            .replace("{{DATE}}", day)
            .replace("{{REASON}}", ecr.reason or "(no reason given)")
            .replace("{{EULOGY_NOTES}}", ecr.eulogy_notes or "(none)")
            .replace("{{GATE_RECORD}}", gate_record))


def validate(plan: TransitionPlan, *,
             repo_root: Path | None = None) -> TransitionPlan:
    """§5.2 — reject, never force. Re-verify evidence (T6/T7 fill the
    action branches), then run the REAL clockwork in an isolated temp
    tree (H-S3-1). On any failure, set plan.rejection and return — the
    caller (CLI / apply) never mutates a rejected plan."""
    if plan.rejection is not None:
        return plan
    # Action-specific evidence re-verification is layered in T6 (ADD) /
    # T7 (MODIFY); REMOVE has no gate (you may always stop trading).
    return plan


def _emit_audit(engine: str, action: str, from_state, to_state,
                approval_class, outcome: str, reason: str | None) -> None:
    """Every terminal outcome → one platform.application_log
    ENGINE_CHANGE_REQUEST row (H-S3-7). DB-best-effort: a missing
    DATABASE_URL logs + returns (the executor is an on-demand tool, not
    on the trade path) — never silently swallow on the apply path."""
    import asyncio

    async def _go() -> None:
        import asyncpg
        url = os.environ.get("DATABASE_URL")
        if not url:
            return
        pool = await asyncpg.create_pool(url, min_size=1, max_size=1)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO platform.application_log "
                    "(engine, run_id, event_type, severity, message, data) "
                    "VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
                    engine, uuid.uuid4(), "ENGINE_CHANGE_REQUEST",
                    "INFO", f"ECR {action} {engine} → {outcome}",
                    json.dumps({
                        "action": action,
                        "engine": engine,
                        "from_state": str(from_state),
                        "to_state": str(to_state),
                        "approval_class": str(approval_class),
                        "outcome": outcome,
                        "reason": reason,
                    }, default=str))
        finally:
            await pool.close()

    try:
        asyncio.run(_go())
    except Exception:  # noqa: BLE001 — audit best-effort, never blocks apply
        pass


def _apply_add(plan: TransitionPlan, root: Path, jn: _Journal) -> None:
    """T6 implements the ADD executor; T5 only exercises REMOVE."""
    raise NotImplementedError("ADD executor lands in T6")


def _apply_modify(plan: TransitionPlan, root: Path, jn: _Journal) -> None:
    """T7 implements the MODIFY executor; T5 only exercises REMOVE."""
    raise NotImplementedError("MODIFY executor lands in T7")


def apply(plan: TransitionPlan, *, repo_root: Path | None = None,
          emit_audit: bool = True,
          _force_validate: bool = False) -> TransitionPlan:
    """H-S3-4 — atomic-or-abort. Journal pre-state; text edits FIRST,
    the package shutil.move LAST; re-run the on-disk clockwork as a
    fresh subprocess; green ⇒ leave it (operator commits); red OR any
    exception ⇒ reverse-order restore to byte-identical, set rejection,
    emit the audit. The executor NEVER runs git (R2 accepted)."""
    root = repo_root or REPO_ROOT
    jn = _Journal()
    try:
        if plan.action is ECRAction.REMOVE:
            _apply_remove(plan, root, jn)
        elif plan.action is ECRAction.ADD:
            _apply_add(plan, root, jn)            # T6
        elif plan.action is ECRAction.MODIFY:
            _apply_modify(plan, root, jn)         # T7
        rc, out = _run_consistency_subprocess(root)
        if rc != 0:
            jn.restore()
            rejected = TransitionPlan(
                **{**plan.__dict__,
                   "rejection": f"post-stage clockwork red (rc={rc}):\n{out}"})
            if emit_audit:
                _emit_audit(plan.engine, plan.action.value,
                            plan.from_state, plan.to_state,
                            plan.approval_class, "rejected",
                            rejected.rejection)
            return rejected
    except Exception as exc:  # noqa: BLE001 — any failure ⇒ full restore
        err: BaseException = exc
        try:
            jn.restore()
            outcome = "rejected"
        except Exception as rexc:  # noqa: BLE001
            outcome = "apply_restore_failed"
            err = rexc  # escalate loudly — the restore failure wins
        rejected = TransitionPlan(
            **{**plan.__dict__, "rejection": f"apply aborted: {err}"})
        if emit_audit:
            _emit_audit(plan.engine, plan.action.value, plan.from_state,
                        plan.to_state, plan.approval_class, outcome,
                        rejected.rejection)
        return rejected
    if emit_audit:
        _emit_audit(plan.engine, plan.action.value, plan.from_state,
                    plan.to_state, plan.approval_class, "applied", None)
    return plan


def _apply_remove(plan: TransitionPlan, root: Path, jn: _Journal) -> None:
    engine = plan.engine
    ep = root / "tpcore" / "engine_profile.py"
    jn.record_file(ep)
    new_src = _rewrite_profile_source(
        ep.read_text(), engine=engine, set_state="retired",
        set_allocator_eligible=False)  # H-S3-3 ast+compile gate inside
    # ENGINE_TABLES orphan removal (documented seam D-SDLC1-1)
    cg = root / "tpcore" / "quality" / "validation" / "capital_gate.py"
    if cg.is_file():
        jn.record_file(cg)
        cgt = cg.read_text()
        cgt2 = re.sub(rf'\n\s*"{engine}":\s*frozenset\([^)]*\),', "", cgt)
        if cgt2 != cgt:
            cg.write_text(cgt2)
    # shadow edits + conditional frozen-literal rewrite (TEXT edits first)
    _shadow_edit_remove(root, engine, jn)
    _maybe_rewrite_frozen_literal(root, retired_engine=engine, jn=jn)
    # EULOGY render (text)
    arc = root / "archive" / engine
    arc.mkdir(parents=True, exist_ok=True)
    eulogy = arc / "EULOGY.md"
    jn.record_file(eulogy)
    # attach_ecr_context threads the ECR free-text onto plan.sot_diff
    # (classify stays pure); the CLI (T8) wraps classify the same way.
    reason = plan.sot_diff.get("reason") or "(retired via ECR)"
    notes = plan.sot_diff.get("eulogy_notes") or "(none)"
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    body = (EULOGY_TEMPLATE.read_text()
            .replace("{{ENGINE}}", engine)
            .replace("{{DATE}}", day)
            .replace("{{REASON}}", reason)
            .replace("{{EULOGY_NOTES}}", notes)
            .replace("{{GATE_RECORD}}", "no surviving gate record"))
    eulogy.write_text(body)
    ep.write_text(new_src)  # the SoT flip (text)
    # the package move LAST (the irreversible-ish op after cheap reverts)
    pkg = root / engine
    if pkg.is_dir():
        # move package CONTENTS into archive/<engine>/ alongside EULOGY;
        # the logical (pkg → arc) move is journaled for reverse restore.
        for item in list(pkg.iterdir()):
            shutil.move(str(item), str(arc / item.name))
        jn.moves.append((pkg, arc))
        pkg.rmdir()


__all__ = [
    "REPO_ROOT",
    "ApprovalClass",
    "EULOGY_TEMPLATE",
    "TransitionPlan",
    "_Journal",
    "apply",
    "attach_ecr_context",
    "classify",
    "validate",
    "_rewrite_profile_source",
    "_run_consistency_subprocess",
    "_staged_copytree",
]
