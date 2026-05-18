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


def _staged_copytree(dest: Path, src: Path | None = None) -> Path:
    """copytree the worktree (``src`` or REPO_ROOT) minus
    .git/.venv/__pycache__/backtests (H-S3-1; R3 accepted: O(repo) but
    on-demand, not a daemon). ``src`` lets validate()'s dry-run copy the
    SAME root apply() will mutate (REPO_ROOT in production; an isolated
    synthetic tree under test) — a faithful simulation, never a lie."""
    shutil.copytree(
        src or REPO_ROOT, dest,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "backtests"))
    return dest


EULOGY_TEMPLATE = REPO_ROOT / "tpcore" / "templates" / "eulogy_template.md"


@dataclass
class _Journal:
    """H-S3-4: every touched file's exact prior bytes (or absent) + every
    PER-ITEM move (src→dst), each recorded BEFORE it is performed, so
    apply() can restore byte-identical on red — the reverse-move drags
    back exactly (and only) the original items, never a generated file.

    Ordering invariant: ``ops`` is a single time-ordered list of
    operations. ``restore()`` replays it in strict reverse, so a
    mid-loop failure (some moves done, some not) is still fully
    reversible — every executed move has its journal entry recorded
    before the move ran (closes #C2).
    """
    files: dict[Path, bytes | None] = field(default_factory=dict)
    # time-ordered ops: ("move", src, dst) or ("file", path) — a "file"
    # op points at the snapshot held in ``files`` (prior bytes / None).
    ops: list[tuple[str, Path, Path | None]] = field(default_factory=list)

    def record_file(self, p: Path) -> None:
        """Snapshot ``p``'s exact prior bytes (or None if absent) and
        append a time-ordered file op. A generated file is recorded with
        prior=None at its REAL final path, so restore() unlinks exactly
        that file wherever it ends up (closes #C1: the per-item reverse
        move never drags a generated EULOGY back into the package)."""
        if p not in self.files:
            self.files[p] = p.read_bytes() if p.is_file() else None
        self.ops.append(("file", p, None))

    def record_move(self, src: Path, dst: Path) -> None:
        """Journal a single src→dst move BEFORE it is performed (the
        caller does the ``shutil.move`` only after this returns)."""
        self.ops.append(("move", src, dst))

    def record_mkdir(self, d: Path) -> None:
        """Journal a directory creation BEFORE it is performed iff ``d``
        is genuinely new. On restore the dir is removed (it must be empty
        once the moves it received are reversed) so NO empty
        ``archive/<engine>/`` is left behind (the byte-identical /
        no-file-stranded invariant covers the dir too)."""
        if not d.exists():
            self.ops.append(("mkdir", d, None))

    def restore(self) -> None:
        """Replay every recorded op in strict reverse order. Moves are
        reversed exactly (dst→src) so only the originally-present items
        return to the package; file ops restore prior bytes or unlink a
        created file at its real path; a journaled mkdir is removed once
        emptied by the reversed moves."""
        for kind, a, b in reversed(self.ops):
            if kind == "move":
                src, dst = a, b
                assert dst is not None
                if src.name == "__sentinel_absent__":
                    # ADD scaffold (T6): no prior package — ``dst`` is a
                    # freshly-created dir/file. Restore = remove it
                    # entirely so a failed ADD leaves ZERO trace (no
                    # stray <engine>/ package). rmtree (not unlink) is
                    # defensive — the scaffold copytree is a dir tree.
                    if dst.exists():
                        if dst.is_dir():
                            shutil.rmtree(dst)
                        else:
                            dst.unlink()
                    continue
                if dst.exists():
                    if src.exists():
                        if src.is_dir():
                            shutil.rmtree(src)
                        else:
                            src.unlink()
                    src.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dst), str(src))
            elif kind == "mkdir":
                # the dir was created by apply(); the reversed moves +
                # file-unlinks above already emptied it — remove it so
                # nothing is stranded. rmtree (not rmdir) is defensive:
                # any residue is itself a rollback defect we must clear.
                if a.exists():
                    shutil.rmtree(a)
            else:  # "file"
                prior = self.files.get(a)
                if prior is None:
                    if a.is_file():
                        a.unlink()
                else:
                    a.write_bytes(prior)


def _shadow_edit_remove(staged: Path, engine: str, jn: _Journal) -> None:
    """Purge the engine from the non-Python shadows. SP4 T5: the new
    shadow text is computed by the ONE renderer
    (scripts.gen_engine_manifest.render_all) — there is exactly one
    mechanism that knows how a shadow is shaped. This SUBSUMES (does
    NOT keep alongside) both the legacy single-line ``.replace`` purges
    AND the T2 DDF-1 fence-aware ``re.sub`` purge — one mechanism, no
    double-apply. The journal+write ordering is UNCHANGED (record_file
    BEFORE write_text); the renderer is pure str→str and is NEVER
    given a path / NEVER writes (H-S4-1). The renderer owns all four
    fenced shadows (run_smoke_test.sh, run_all_engines.sh,
    ops/platform_pipeline.py, pyproject.toml) so a REMOVE regenerates
    every one — the widened folded leg-6 (T4) then passes post-REMOVE.

    Lazy in-body import (H-S4-9 / spec §14 — no eager engine-or-
    collision import at planner module top; the generator imports only
    ``tpcore.engine_profile`` + stdlib, never ``ops``)."""
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT))
    from scripts.gen_engine_manifest import _FILE_REGIONS, render_all
    from tpcore.engine_profile import archived_engines, roster_for_dispatch
    # REMOVE drops `engine` from the roster; the SoT flip to RETIRED
    # happens later in _apply_remove (the engine_profile.py rewrite),
    # so derive the post-removal roster by filtering it out of the
    # current roster.
    post_roster = tuple(e for e in roster_for_dispatch() if e != engine)
    archived = archived_engines()
    for rel in _FILE_REGIONS:
        p = staged / rel
        jn.record_file(p)
        p.write_text(render_all(p.read_text(), rel,
                                post_roster, archived))


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


def _render_eulogy(engine: str, *, reason: str, eulogy_notes: str,
                   gate_record: str) -> str:
    """#N1: the SINGLE eulogy template-fill site (DRY) — _apply_remove
    routes its eulogy generation through here. Takes resolved strings
    (not an ECR) so the apply path, which threads the ECR free-text via
    plan.sot_diff, and any future caller share one template contract."""
    tmpl = EULOGY_TEMPLATE.read_text()
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    return (tmpl
            .replace("{{ENGINE}}", engine)
            .replace("{{DATE}}", day)
            .replace("{{REASON}}", reason or "(no reason given)")
            .replace("{{EULOGY_NOTES}}", eulogy_notes or "(none)")
            .replace("{{GATE_RECORD}}", gate_record))


def _stage_proposed_edits(plan: TransitionPlan, root: Path,
                          jn: _Journal) -> None:
    """The SINGLE staging dispatch (spec §5.2 step 2 / H-S3-1): apply the
    EXACT proposed edits the executor would write into ``root``. Both
    ``validate()``'s isolated dry-run (root=an ephemeral copytree, a
    throwaway journal) and ``apply()``'s real run (root=REPO_ROOT, the
    real rollback journal) route through the SAME ``_apply_*`` functions —
    so the dry run is a FAITHFUL simulation of exactly what apply() will
    write (it cannot diverge / lie). The journal is the rollback contract
    of the REAL path only; the copytree dry-run's journal is discarded
    untouched (the whole temp tree is removed)."""
    if plan.action is ECRAction.REMOVE:
        _apply_remove(plan, root, jn)
    elif plan.action is ECRAction.ADD:
        _apply_add(plan, root, jn)
    elif plan.action is ECRAction.MODIFY:
        _apply_modify(plan, root, jn)


def _dry_consistency_run(plan: TransitionPlan,
                         repo_root: Path | None = None) -> str | None:
    """Spec §3.2 / §5.2 step 2 / H-S3-1 / H-S3-7(b): BEFORE the operator
    y/n, copytree the SAME worktree apply() will mutate (``repo_root`` or
    REPO_ROOT) into an isolated temp tree, stage the SAME proposed edits
    ``apply()`` would write into THAT copy only, and run the REAL
    ``test_engine_lifecycle_consistency.py`` clockwork as a fresh
    subprocess with cwd=the temp tree. Returns the clockwork failure
    text on rc≠0 (the caller turns it into ``plan.rejection`` — so the
    operator only ever confirms a green-validated diff), else None. The
    real repo is NEVER mutated; the temp tree is always cleaned up. A
    wedged subprocess raises (bounded) → propagates as a reject, never a
    false-green."""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="ecr_dryrun_"))
    try:
        staged = _staged_copytree(tmp / "tree", repo_root or REPO_ROOT)
        # a THROWAWAY journal — the dry run never rolls back (the whole
        # temp tree is rmtree'd); the journal exists only because the
        # shared _apply_* staging functions take one.
        try:
            _stage_proposed_edits(plan, staged, _Journal())
        except Exception as exc:  # noqa: BLE001 — a staging failure IS a
            # validation failure (apply() would abort identically). The
            # dry run NEVER lets it escape validate() as a false-green or
            # an uncaught raise — it is faithfully reported as a reject.
            return (f"pre-approval dry run could not stage the proposed "
                    f"diff (apply() would abort identically); nothing "
                    f"was mutated: {exc}")
        rc, out = _run_consistency_subprocess(staged)
        if rc != 0:
            return (f"pre-approval dry consistency run RED (rc={rc}) — "
                    f"the proposed diff fails the REAL "
                    f"test_engine_lifecycle_consistency.py clockwork; "
                    f"nothing was mutated:\n{out}")
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def validate(plan: TransitionPlan, *, repo_root: Path | None = None,
             ecr: EngineChangeRequest | None = None) -> TransitionPlan:
    """§5.2 — reject, never force. ADD: H-S3-11 fail-closed gate.
    MODIFY: H-S3-6 zero-trust (T7). REMOVE: no gate (always may stop).
    Then (§3.2/§5.2 step 2/H-S3-1) the spec-mandated PRE-APPROVAL
    isolated dry consistency run for every mutating action — so the
    operator only ever confirms a green-validated diff (a red dry run
    is a hard reject; the CLI never prints a fabricated GREEN)."""
    if plan.rejection is not None:
        return plan
    # NOTE (planner.py:390 Nit, T6-review carry-forward): the prior
    # `root = repo_root or REPO_ROOT; del root` motion was a placeholder
    # for "the T7 MODIFY branch will need the staged tree". It does NOT:
    # `_validate_modify` re-derives every gate number from the FROZEN
    # LabResult JSON sidecar at the ECR-cited path (zero-trust H-S3-6) —
    # no staged-tree read on the validate() side. The dead motion +
    # comment are removed; `repo_root` stays an accepted kwarg for the
    # ADD readiness path / API parity (apply()/promote() own the tree).
    if plan.action is ECRAction.ADD and ecr is not None:
        if ecr.source == "new_scaffold":
            if ecr.gate_dsr is not None or ecr.gate_cred is not None:
                return _reject(
                    ecr, "new_scaffold ADD must NOT carry gate_dsr/"
                         "gate_cred — a new engine cannot present a gate "
                         "score it has not earned (fail-closed H-S3-11b)")
        elif ecr.source == "lab_candidate":
            from ops.engine_sdlc._evidence import (
                EvidenceError,
                assert_identity_fresh,
                load_labresult_sidecar,
            )
            try:
                lr = load_labresult_sidecar(ecr.lab_dossier)
                # §5.4 / H-S3-6b: a valid sidecar from a DIFFERENT Lab
                # run sitting at the cited path is a hard reject.
                assert_identity_fresh(lr, ecr.lab_dossier)
            except EvidenceError as exc:
                return _reject(ecr, str(exc))
            if lr.recommended_exit == "fold_existing":
                return _reject(
                    ecr, "lab_candidate dossier recommends fold_existing "
                         "— that is a MODIFY of the target engine, NOT an "
                         "ADD (H-S3-11c). Re-file as action: MODIFY.")
            if not (lr.verdict == "SURVIVED" and lr.dsr >= 0.95
                    and lr.credibility_score >= 60
                    and lr.recommended_exit == "promote_new"):
                return _reject(
                    ecr, f"lab_candidate sidecar fails the gate: "
                         f"verdict={lr.verdict} dsr={lr.dsr} "
                         f"cred={lr.credibility_score} "
                         f"recommended_exit={lr.recommended_exit}")
        if plan.to_state is not LifecycleState.LAB:
            return _reject(ecr, "ADD must land LAB, never PAPER (H-S3-11a)")
    if plan.action is ECRAction.MODIFY and ecr is not None:
        plan = _validate_modify(plan, ecr)  # T7 zero-trust
        if plan.rejection is not None:
            return plan
    # §3.2/§5.2 step 2/H-S3-1/H-S3-7(b): the spec-mandated PRE-APPROVAL
    # isolated dry consistency run for every MUTATING action — staged
    # into an ephemeral copytree (the real repo is never touched), the
    # SAME edits apply() would write, the REAL clockwork as a fresh
    # subprocess. A red is a hard reject (the operator only ever
    # confirms a green-validated diff; the CLI never fabricates GREEN).
    if plan.action in (ECRAction.ADD, ECRAction.REMOVE, ECRAction.MODIFY):
        dry = _dry_consistency_run(plan, repo_root)
        if dry is not None:
            return _reject(ecr, dry) if ecr is not None else TransitionPlan(
                **{**plan.__dict__, "rejection": dry})
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


_READINESS_PLUG_RE = re.compile(r"class\s+\w+\(BaseEnginePlug\)")


def _check_readiness(staged: Path, engine: str) -> str | None:
    """The programmatically-checkable engine_readiness.md items
    (H-S3-11d). Returns a rejection reason or None."""
    pkg = staged / engine
    if not pkg.is_dir():
        return f"readiness: scaffold {engine}/ not created"
    if not (pkg / "tests").is_dir():
        return f"readiness: {engine}/tests/ missing (engine_readiness §6)"
    try:
        import importlib.util
        spec = importlib.util.find_spec(f"{engine}.scheduler")
    except ModuleNotFoundError:
        spec = None
    if spec is None and not (pkg / "scheduler.py").is_file():
        return f"readiness: {engine}.scheduler not importable"
    plug_count = sum(
        len(_READINESS_PLUG_RE.findall(p.read_text()))
        for p in (pkg / "plugs").glob("*.py")) if (
            pkg / "plugs").is_dir() else 0
    if plug_count != 5:
        return (f"readiness: expected 5 BaseEnginePlug subclasses in "
                f"{engine}/plugs/, found {plug_count}")
    return None


def _apply_add(plan: TransitionPlan, root: Path, jn: _Journal) -> None:
    """H-S3-11 ADD executor (new_scaffold / lab_candidate). ADD ALWAYS
    lands LAB (allocator_eligible forced False — SP1 test_no_half_state);
    the AST-safe _PROFILE insert goes IMMEDIATELY before the documented
    ``allocator`` sentinel comment (H-S3-3 stable anchor). Every scaffold
    copy / file write is journaled BEFORE it happens so a red consistency
    subprocess OR any exception reverse-replays to a BYTE-IDENTICAL
    pre-state (H-S3-4) — a failed ADD leaves ZERO trace (no stray
    package, no _PROFILE entry, no scaffold residue). It reuses the T5
    ``_Journal`` (action-agnostic record_move/record_file) — the
    scaffold-copy is recorded as a ``__sentinel_absent__`` move whose
    restore is ``rmtree(dst)`` (there is no prior package).
    """
    engine = plan.engine
    src_tmpl = root / "tpcore" / "templates" / "engine_template"
    if not src_tmpl.is_dir():
        raise RuntimeError(
            "engine_template scaffold missing — cannot ADD(new_scaffold)")
    pkg = root / engine
    if pkg.exists():
        raise RuntimeError(
            f"ADD target {engine}/ already exists on disk — refusing to "
            f"clobber (classify should have rejected; defence-in-depth)")
    # scaffold: there is no prior package, so the whole new tree is
    # journaled as one sentinel-move (restore = rmtree pkg). Journal
    # BEFORE the copytree so a failure mid-copy is still fully reversible.
    jn.record_move(pkg / "__sentinel_absent__", pkg)
    shutil.copytree(src_tmpl, pkg)
    # AST-safe _PROFILE insert BEFORE the allocator sentinel comment.
    ep = root / "tpcore" / "engine_profile.py"
    jn.record_file(ep)
    cad = plan.sot_diff.get("cadence") or "daily"
    order = plan.sot_diff.get("dispatch_order")
    cad_enum = {
        "daily": "Cadence.DAILY",
        "weekly_first_trading_day": "Cadence.WEEKLY_FIRST_TRADING_DAY",
        "monthly_first_trading_day": "Cadence.MONTHLY_FIRST_TRADING_DAY",
    }[cad]
    new_entry = (
        f'    "{engine}":   EngineProfile(engine="{engine}", '
        f'cadence={cad_enum},\n'
        f'                               dispatch_order={order}, '
        f'lifecycle_state=LifecycleState.LAB),\n')
    src = ep.read_text()
    anchor = "    # allocator: separate _dispatch_allocator path"
    if anchor not in src:
        raise RuntimeError("allocator sentinel anchor not found in _PROFILE")
    new_src = src.replace(anchor, new_entry + anchor, 1)
    compile(new_src, "<engine_profile_add>", "exec")  # H-S3-3 gate
    miss = _check_readiness(root, engine)  # readiness BEFORE the SoT write
    if miss is not None:
        raise RuntimeError(miss)  # apply()'s except → full restore
    ep.write_text(new_src)


def _validate_modify(plan: TransitionPlan,
                     ecr: EngineChangeRequest) -> TransitionPlan:
    """H-S3-6 zero-trust: the gate is the ONLY thing between a dossier
    and live params, so re-derive every number from the FROZEN JSON
    sidecar, never the ECR text / rendered markdown."""
    from ops.engine_sdlc._evidence import (
        EvidenceError,
        assert_identity_fresh,
        load_labresult_sidecar,
    )
    from ops.lab.run import PARAM_RANGES
    try:
        lr = load_labresult_sidecar(ecr.lab_dossier)
        # §5.4 / H-S3-6b: identity-freshness — a valid sidecar from a
        # DIFFERENT Lab run at the cited path is a hard reject (the only
        # thing between a dossier and live params is this gate).
        assert_identity_fresh(lr, ecr.lab_dossier)
    except EvidenceError as exc:
        return _reject(ecr, str(exc))
    if lr.verdict != "SURVIVED":
        return _reject(ecr, f"sidecar verdict {lr.verdict} != SURVIVED")
    if lr.dsr < 0.95:
        return _reject(ecr, f"sidecar dsr {lr.dsr} < 0.95 (forged/stale)")
    if lr.credibility_score < 60:
        return _reject(ecr, f"sidecar credibility {lr.credibility_score} "
                            f"< 60")
    if lr.recommended_exit != "fold_existing":
        return _reject(
            ecr, f"sidecar recommended_exit {lr.recommended_exit!r} != "
                 f"fold_existing (a promote_new is an ADD, not a MODIFY)")
    if lr.target_engine != ecr.engine:
        return _reject(
            ecr, f"sidecar target_engine {lr.target_engine!r} != ECR "
                 f"engine {ecr.engine!r} (wrong-target reject)")
    ranges = PARAM_RANGES.get(ecr.engine, {})
    for k, v in (ecr.param_change or {}).items():
        if k not in ranges:
            return _reject(
                ecr, f"param {k!r} not in {ecr.engine} PARAM_RANGES — "
                     f"the Lab never swept it (no-smuggle H-S3-6c)")
        if k not in lr.winning_params:
            return _reject(
                ecr, f"param {k!r} not in the sidecar winning_params")
        # value-equality (coerce the ECR string to the sidecar's type)
        want = lr.winning_params[k]
        try:
            got = type(want)(v)
        except (TypeError, ValueError):
            got = v
        if got != want:
            return _reject(
                ecr, f"param {k!r} value mismatch: ECR={v!r} vs sidecar "
                     f"winning {want!r}")
    if plan.sot_diff and any(
            kk in plan.sot_diff for kk in (
                "lifecycle_state", "allocator_eligible",
                "dispatch_order", "cadence")):
        return _reject(ecr, "MODIFY plan carries a _PROFILE edit — "
                            "lifecycle is immutable under MODIFY "
                            "(H-S3-6d)")
    return plan


def _apply_modify(plan: TransitionPlan, root: Path, jn: _Journal) -> None:
    """Apply the validated current→winning diff to the engine's
    default_params() SOURCE (the O1 seam). _PROFILE is NEVER touched
    (H-S3-6d). Line-anchored edit of the engine backtest.py/models.py
    default constants, AST-validated."""
    engine = plan.engine
    consts = _ENGINE_DEFAULT_CONSTS.get(engine)
    if consts is None:
        raise RuntimeError(
            f"no MODIFY default-constant map for engine {engine!r}")
    pc = plan.sot_diff.get("param_change") or {}
    # Group the param edits by their target source file (per the
    # executor note: reversion z_threshold lives in reversion/models.py,
    # not reversion/backtest.py — the line-anchored edit must hit the
    # file the default_params() accessor actually reads).
    by_file: dict[Path, dict[str, str]] = {}
    for key, raw in pc.items():
        spec = consts.get(key)
        if spec is None:
            raise RuntimeError(
                f"no module default seam for {key!r} on {engine}; not "
                f"MODIFY-able via the constant path")
        rel, const_name = spec
        tgt = root / rel
        by_file.setdefault(tgt, {})[const_name] = str(raw)
    for tgt, edits in by_file.items():
        if not tgt.is_file():
            raise RuntimeError(
                f"{tgt.relative_to(root)} not found for {engine} MODIFY")
        jn.record_file(tgt)
        src = tgt.read_text()
        new_src = src
        for const_name, raw in edits.items():
            pat = re.compile(
                rf"^({re.escape(const_name)}\s*=\s*)([^\n#]+)", re.M)
            m = pat.search(new_src)
            if not m:
                raise RuntimeError(
                    f"default constant {const_name} not found in "
                    f"{tgt.relative_to(root)}")
            new_src = pat.sub(rf"\g<1>{raw}", new_src, count=1)
        compile(new_src, "<backtest_modify>", "exec")  # AST gate
        tgt.write_text(new_src)


# The engine PARAM_RANGES key → (source file relative to repo root,
# module-level UPPER_CASE default constant the engine's default_params()
# accessor reads). VERIFIED against the live source (per the plan's T7
# executor note — "run/grep each engine's source for the exact
# constant"): reversion.backtest.default_params() reads
# Z_SCORE_THRESHOLD (imported from reversion/models.py — value lives
# THERE, so the z_threshold MODIFY must edit reversion/models.py),
# while VOLUME_CLIMAX_MULTIPLIER_DEFAULT / MAX_HOLD_DAYS / HARD_STOP_PCT
# are module defaults in reversion/backtest.py. Per-key target file is
# the whole point of the (file, const) tuple. The T7 fixtures only
# exercise z_threshold + max_hold_days on reversion, so reversion's map
# is exactly right; other engines added on their MODIFY rollout.
_ENGINE_DEFAULT_CONSTS: dict[str, dict[str, tuple[str, str]]] = {
    "reversion": {
        "z_threshold": ("reversion/models.py", "Z_SCORE_THRESHOLD"),
        "volume_climax_multiplier": (
            "reversion/backtest.py", "VOLUME_CLIMAX_MULTIPLIER_DEFAULT"),
        "max_hold_days": ("reversion/backtest.py", "MAX_HOLD_DAYS"),
        "stop_pct": ("reversion/backtest.py", "HARD_STOP_PCT"),
    },
}


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
    # The package CONTENTS move into archive/<engine>/ alongside a
    # generated EULOGY. Atomicity (H-S3-4, #C1/#C2): every per-item move
    # is journaled BEFORE it is performed so a mid-loop failure is fully
    # reversible (#C2); the generated EULOGY is journaled as its own
    # file entry (prior=None) at its REAL final path so restore() unlinks
    # exactly that file and the per-item reverse-move never drags it back
    # into the package (#C1) — the restored package is byte-identical.
    arc = root / "archive" / engine
    jn.record_mkdir(arc)  # journal-before-create so a red removes it
    arc.mkdir(parents=True, exist_ok=True)
    pkg = root / engine
    if pkg.is_dir():
        # journal-then-move each original package item individually.
        for item in list(pkg.iterdir()):
            jn.record_move(item, arc / item.name)
            shutil.move(str(item), str(arc / item.name))
        pkg.rmdir()
    # EULOGY render (text) — its own journal entry at its real path.
    eulogy = arc / "EULOGY.md"
    jn.record_file(eulogy)
    # attach_ecr_context threads the ECR free-text onto plan.sot_diff
    # (classify stays pure); the CLI (T8) wraps classify the same way.
    eulogy.write_text(_render_eulogy(
        engine,
        reason=plan.sot_diff.get("reason") or "(retired via ECR)",
        eulogy_notes=plan.sot_diff.get("eulogy_notes") or "(none)",
        gate_record="no surviving gate record"))
    ep.write_text(new_src)  # the SoT flip (text)


def promote(engine: str, *, repo_root: Path | None = None,
            emit_audit: bool = True,
            _gate_green: bool | None = None) -> TransitionPlan:
    """LAB→PAPER — automated, gated, NOT an ECR action (spec §4.1).
    Flips iff the capital-gate/graduation_ready authority is green. The
    test seam ``_gate_green`` injects the verdict offline; production
    resolves it via the real authority. Reuses the T5 ``_Journal``
    byte-identical-rollback discipline + the T4 ``_rewrite_profile_source``
    (the ONLY _PROFILE editor) + ``_run_consistency_subprocess`` — a
    post-flip red OR any exception reverse-replays to byte-identical
    (the LAB engine stays LAB, ZERO trace)."""
    root = repo_root or REPO_ROOT
    if _gate_green is None:
        from tpcore.quality.validation.capital_gate import (
            ENGINE_TABLES,  # noqa: F401 — presence import; real gate below
        )
        # production: resolve graduation_ready(pool, engine) via the SP2
        # lab.<candidate> credibility namespace; deferred to the CLI (T8)
        # which owns the pool. Here require an explicit resolved verdict —
        # a promote without a resolved gate is a hard reject, never a
        # silent flip (H-S3-6 zero-trust parity with the MODIFY gate).
        return TransitionPlan(
            action=ECRAction.MODIFY, engine=engine, from_state=None,
            to_state=None, approval_class=ApprovalClass.AUTOMATED,
            rejection="promote requires a resolved gate verdict")
    plan = TransitionPlan(
        action=ECRAction.MODIFY, engine=engine,
        from_state=LifecycleState.LAB, to_state=LifecycleState.PAPER,
        approval_class=ApprovalClass.AUTOMATED)
    if not _gate_green:
        rej = TransitionPlan(**{**plan.__dict__,
                                "rejection": "capital-gate/graduation_ready "
                                             "RED — LAB→PAPER refused"})
        if emit_audit:
            _emit_audit(engine, "promote", "lab", "paper",
                        "AUTOMATED", "rejected", rej.rejection)
        return rej
    ep = root / "tpcore" / "engine_profile.py"
    jn = _Journal()
    jn.record_file(ep)
    try:
        new = _rewrite_profile_source(
            ep.read_text(), engine=engine, set_state="paper",
            set_allocator_eligible=False)
        ep.write_text(new)
        rc, out = _run_consistency_subprocess(root)
        if rc != 0:
            jn.restore()
            rej = TransitionPlan(
                **{**plan.__dict__,
                   "rejection": f"post-flip clockwork red:\n{out}"})
            if emit_audit:
                _emit_audit(engine, "promote", "lab", "paper",
                            "AUTOMATED", "rejected", rej.rejection)
            return rej
    except Exception as exc:  # noqa: BLE001
        jn.restore()
        rej = TransitionPlan(**{**plan.__dict__,
                                "rejection": f"promote aborted: {exc}"})
        if emit_audit:
            _emit_audit(engine, "promote", "lab", "paper", "AUTOMATED",
                        "rejected", rej.rejection)
        return rej
    if emit_audit:
        _emit_audit(engine, "promote", "lab", "paper", "AUTOMATED",
                    "applied", None)
    return plan


__all__ = [
    "REPO_ROOT",
    "ApprovalClass",
    "EULOGY_TEMPLATE",
    "TransitionPlan",
    "_Journal",
    "apply",
    "attach_ecr_context",
    "classify",
    "promote",
    "validate",
    "_apply_modify",
    "_rewrite_profile_source",
    "_run_consistency_subprocess",
    "_staged_copytree",
    "_validate_modify",
]
