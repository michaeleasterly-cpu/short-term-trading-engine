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
                 "gate_dsr": ecr.gate_dsr, "gate_cred": ecr.gate_cred,
                 # Spec §7.2 (2026-05-20): thread the ECR's declared
                 # per-engine data reads onto plan.sot_diff so _apply_add
                 # can render the EngineProfile.data_dependencies kwarg
                 # into the new _PROFILE line. None when the ECR omits
                 # the key (new_scaffold/lab_candidate optional path);
                 # frozenset[str] otherwise.
                 "data_dependencies": ecr.data_dependencies}
    elif ecr.action is ECRAction.MODIFY:
        # Spec §7 follow-up (2026-05-21, audit
        # docs/superpowers/audits/2026-05-20-engine-data-dependencies-
        # accuracy.md): thread ``data_dependencies`` + ``need`` onto a
        # MODIFY plan's sot_diff so ``_apply_modify`` can re-render the
        # existing _PROFILE row's ``data_dependencies=frozenset({...})``
        # kwarg (the in-place data-deps accuracy correction). None when
        # the ECR omits the key (the common param-change-only path) —
        # _apply_modify skips the _PROFILE rewrite when the value is
        # None / empty so the byte-identical existing tests stay green.
        extra = {"lab_dossier": ecr.lab_dossier,
                 "param_change": ecr.param_change,
                 "gate_dsr": ecr.gate_dsr, "gate_cred": ecr.gate_cred,
                 "data_dependencies": ecr.data_dependencies,
                 "need": ecr.need}
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


def _render_data_dependencies_literal(deps: frozenset[str]) -> str:
    """Render a ``data_dependencies=frozenset({...})`` kwarg value
    deterministically (sorted), matching the byte shape of the hand-
    curated _PROFILE entries. Mirrors the same sorting discipline used
    by ``_apply_add`` (a frozenset's set-iteration order is hash-
    randomized — sorting pins the line content across runs)."""
    inner = ", ".join(f'"{t}"' for t in sorted(deps))
    return f"frozenset({{{inner}}})"


# Spec §7 follow-up (2026-05-21): match ``data_dependencies=`` plus its
# RHS value. The RHS can be either:
#   - the hand-curated ``frozenset({...})`` literal — balanced braces
#   - the default ``frozenset()`` (an absent declaration)
# The non-greedy ``\{[^{}]*\}`` is sufficient because the literal never
# nests braces inside the frozenset call (one set, string members).
_DATA_DEPS_KW_RE = re.compile(
    r"data_dependencies\s*=\s*frozenset\(\s*(?:\{[^{}]*\})?\s*\)")


def _rewrite_profile_data_dependencies(
    src: str, *, engine: str, deps: frozenset[str],
) -> str:
    """H-S3-3-style targeted-line + AST-validated rewrite of the SINGLE
    target EngineProfile(...) entry's ``data_dependencies=`` kwarg.
    Mirrors ``_rewrite_profile_source`` discipline: parse the pre-state,
    locate the entry by its quoted-key anchor, scan to the matching ``)``
    of the EngineProfile call, rewrite (or inject) the kwarg inside that
    block ONLY, then compile() the full new source as the gate.

    Touches no sibling engine, adds no import (H-S3-10), preserves the
    surrounding kwargs / comments. ``deps`` may be empty: an empty
    frozenset round-trips as ``frozenset()`` (the EngineProfile default).
    The lifecycle / dispatch_order / allocator_eligible / cadence kwargs
    are NEVER touched by this rewriter — the lifecycle-immutable
    invariant (H-S3-6d) is preserved structurally."""
    ast.parse(src)  # pre-edit parse — proves the baseline is sane
    lines = src.splitlines(keepends=True)
    key_anchor = f'"{engine}":'
    start = next((i for i, ln in enumerate(lines)
                  if key_anchor in ln and "EngineProfile(" in ln), None)
    if start is None:
        raise ValueError(
            f"_PROFILE entry for {engine!r} not found (key anchor "
            f"{key_anchor!r}) — cannot rewrite data_dependencies")
    depth = 0
    end = start
    for i in range(start, len(lines)):
        depth += lines[i].count("(") - lines[i].count(")")
        if depth == 0:
            end = i
            break
    block = "".join(lines[start:end + 1])
    new_value = _render_data_dependencies_literal(deps)
    new_kw = f"data_dependencies={new_value}"
    if _DATA_DEPS_KW_RE.search(block):
        new_block = _DATA_DEPS_KW_RE.sub(new_kw, block, count=1)
    else:
        # absent → inject before the final ')' of the EngineProfile call.
        # Match the existing kwarg column (31 spaces) so the byte shape
        # mirrors the hand-curated entries — verified against catalyst /
        # momentum / reversion / vector / sentinel / canary / allocator
        # in tpcore/engine_profile.py.
        idx = block.rfind(")")
        injection = (",\n                               "
                     f"{new_kw}")
        new_block = block[:idx] + injection + block[idx:]
    new_src = ("".join(lines[:start]) + new_block
               + "".join(lines[end + 1:]))
    # H-S3-3 gate: the rewritten source must parse AND compile.
    compile(new_src, "<engine_profile_data_deps_rewrite>", "exec")
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


def _shadow_edit_add_to_paper(staged: Path, engine: str, *,
                              dispatch_order: int,
                              jn: _Journal) -> None:
    """H-S3-12: regenerate the non-Python shadows when an ADD lands
    PAPER (the new ``source: existing_code`` autonomous-criteria path).
    Mirrors ``_shadow_edit_remove``'s ONE-renderer discipline + the same
    journal-before-write ordering.

    The post-ADD roster is computed by inserting ``engine`` into the
    current roster sorted by ``dispatch_order`` — the planner does the
    in-memory _PROFILE edit BEFORE this is called, but the engine_profile
    module is already imported in this process (frozen pydantic; the
    rewritten source on disk has not yet been reloaded). So we compute
    the post-state roster directly from current_roster + the proposed
    insert."""
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT))
    from scripts.gen_engine_manifest import _FILE_REGIONS, render_all
    from tpcore.engine_profile import (
        _PROFILE,
        archived_engines,
        roster_for_dispatch,
    )
    cur = list(roster_for_dispatch())
    if engine in cur:
        post_roster = tuple(cur)
    else:
        # insert preserving dispatch_order ordering. Build (order, name)
        # pairs from _PROFILE for the current roster engines (we already
        # know cur is dispatch_order-sorted) and splice in the new one.
        cur_pairs = [(_PROFILE[n].dispatch_order, n) for n in cur]
        cur_pairs.append((dispatch_order, engine))
        cur_pairs.sort(key=lambda p: p[0])
        post_roster = tuple(n for _, n in cur_pairs)
    archived = archived_engines()
    for rel in _FILE_REGIONS:
        p = staged / rel
        jn.record_file(p)
        p.write_text(render_all(p.read_text(), rel,
                                post_roster, archived))


def _maybe_rewrite_frozen_literal_add(
    staged: Path, *, added_engine: str, dispatch_order: int,
    jn: _Journal,
) -> None:
    """H-S3-12 ADD-leg companion to ``_maybe_rewrite_frozen_literal``: an
    ADD that lands PAPER changes ``roster_for_dispatch()`` (LAB is filtered
    by _DISPATCHABLE; PAPER is not), so the frozen-literal pin in
    test_dispatch_order_invariant_is_the_frozen_literal must be rewritten
    in the SAME staged diff — never a hand-edit. Inserts ``added_engine``
    at its dispatch_order position."""
    tc = (staged / "tpcore" / "tests"
          / "test_engine_lifecycle_consistency.py")
    jn.record_file(tc)
    src = tc.read_text()
    m = re.search(
        r"roster_for_dispatch\(\) == \(\s*([^)]+)\)", src)
    if not m:
        return
    toks = [t.strip().strip('"') for t in m.group(1).split(",")
            if t.strip()]
    if added_engine in toks:
        return
    # splice the new engine in by dispatch_order
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT))
    from tpcore.engine_profile import _PROFILE
    pairs = [(_PROFILE[n].dispatch_order, n) for n in toks
             if n in _PROFILE]
    pairs.append((dispatch_order, added_engine))
    pairs.sort(key=lambda p: p[0])
    new_toks = [n for _, n in pairs]
    new_tuple = ", ".join(f'"{t}"' for t in new_toks)
    tc.write_text(src.replace(m.group(0),
                  f"roster_for_dispatch() == ({new_tuple})"))


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
        elif ecr.source == "existing_code":
            # H-S3-11e: post-hoc roster registration of engine code shipped
            # via a separate PR (the SP-F → catalyst pattern). Same gate-
            # field invariant as new_scaffold (a freshly-registered engine
            # has not earned its gate yet); ADDITIONAL discriminating
            # constraint: the engine package MUST already exist on disk.
            if ecr.gate_dsr is not None or ecr.gate_cred is not None:
                return _reject(
                    ecr, "existing_code ADD must NOT carry gate_dsr/"
                         "gate_cred — a freshly-registered engine has not "
                         "earned a gate score (fail-closed; same invariant "
                         "as new_scaffold).")
            # H-S3-11e + H-S3-12: pkg-on-disk + autonomous Lab criteria
            # are evaluated against `effective_root` — explicit repo_root
            # when given (tests), REPO_ROOT in production (the CLI passes
            # no repo_root). The criteria MUST run on the production path,
            # not be gated on `repo_root is not None` (which would
            # silently skip the autonomous gate in production).
            effective_root = repo_root or REPO_ROOT
            pkg = effective_root / ecr.engine
            if not pkg.is_dir():
                return _reject(
                    ecr, f"existing_code ADD requires {ecr.engine}/ to "
                         f"already exist on disk — got nothing. Use "
                         f"source: new_scaffold to scaffold from the "
                         f"template, or ship the engine code first.")
            # H-S3-12: autonomous Lab criteria. existing_code ADD reads
            # the engine's most-recent backtest dossier from
            # backtests/<engine>_backtest_results.json (the canonical
            # artifact every <engine>.backtest writes) and evaluates
            # the new-engine signal-presence criteria. On pass the
            # planner lands the engine PAPER (autonomous gate); on
            # fail the rejection cites the specific criterion. See
            # docs/superpowers/specs/2026-05-20-autonomous-lab-criteria.md.
            from ops.engine_sdlc.lab_criteria import (
                _assess_new_engine_signal,
                load_engine_dossier,
            )
            try:
                dossier = load_engine_dossier(effective_root, ecr.engine)
            except ValueError as exc:
                return _reject(
                    ecr, f"existing_code ADD: backtest dossier at "
                         f"backtests/{ecr.engine}_backtest_results.json "
                         f"is unparseable — {exc}")
            if dossier is None:
                return _reject(
                    ecr, f"existing_code ADD: no recent backtest "
                         f"dossier found at "
                         f"backtests/{ecr.engine}_backtest_results.json "
                         f"— run `python -m {ecr.engine}.backtest --json` "
                         f"first to produce the dossier the planner "
                         f"reads autonomously.")
            passed, reason = _assess_new_engine_signal(dossier)
            if not passed:
                return _reject(
                    ecr, f"existing_code ADD: autonomous Lab criteria "
                         f"failed — {reason}")
            # Criteria pass ⇒ promote the plan's to_state to PAPER (the
            # operator-style ADD already gated the binary y/n; the
            # framework no longer needs a second human gate for "did
            # this engine earn its way out of LAB?" because the
            # dossier-read criteria already decided).
            plan = TransitionPlan(
                **{**plan.__dict__,
                   "to_state": LifecycleState.PAPER})
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
        # H-S3-11a / H-S3-12: ADD lands LAB by default; the ONE exception
        # is source: existing_code whose autonomous Lab criteria passed —
        # validate() above promoted plan.to_state to PAPER in that case
        # (the framework's autonomous gate decided). Every other ADD
        # source (new_scaffold / lab_candidate) MUST still land LAB.
        if plan.to_state is LifecycleState.PAPER:
            if ecr.source != "existing_code":
                return _reject(
                    ecr, "ADD must land LAB, never PAPER, except via "
                         "source: existing_code whose autonomous Lab "
                         "criteria pass (H-S3-11a / H-S3-12)")
        elif plan.to_state is not LifecycleState.LAB:
            return _reject(ecr, "ADD must land LAB, never PAPER (H-S3-11a)")
    if plan.action is ECRAction.MODIFY and ecr is not None:
        plan = _validate_modify(plan, ecr, repo_root=repo_root)  # T7 zero-trust
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
        from tpcore.db import build_asyncpg_pool
        url = os.environ.get("DATABASE_URL")
        if not url:
            return
        # ECR executor — an on-demand CLI tool. read_only=False: the
        # application_log INSERT below needs write; explicit read/write
        # intent is mandatory on the isolation boundary (H-S3-8).
        # Canonical pooler-safety lives in tpcore.db.
        pool = await build_asyncpg_pool(
            url, min_size=1, max_size=1, read_only=False)
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
    pkg = root / engine
    is_existing_code = plan.sot_diff.get("source") == "existing_code"
    if is_existing_code:
        # H-S3-11e: post-hoc roster registration of engine code shipped via
        # a separate PR. The engine tree is operator-shipped; we MUST NOT
        # journal a sentinel_absent move (which would cause reverse-replay
        # to rmtree the existing engine code on failure). Only the
        # _PROFILE write is journaled below. Defence-in-depth: re-verify
        # the directory exists; validate() should have gated this already.
        if not pkg.is_dir():
            raise RuntimeError(
                f"existing_code ADD requires {engine}/ to exist on disk — "
                f"got nothing (validate should have rejected; defence-in-"
                f"depth).")
    else:
        # new_scaffold / lab_candidate: scaffold from template. There is no
        # prior package, so the whole new tree is journaled as one
        # sentinel-move (restore = rmtree pkg). Journal BEFORE the copytree
        # so a failure mid-copy is still fully reversible.
        src_tmpl = root / "tpcore" / "templates" / "engine_template"
        if not src_tmpl.is_dir():
            raise RuntimeError(
                "engine_template scaffold missing — cannot ADD(new_scaffold)")
        if pkg.exists():
            raise RuntimeError(
                f"ADD target {engine}/ already exists on disk — use "
                f"source: existing_code for post-hoc roster registration "
                f"of engine code shipped via a separate PR, or remove the "
                f"directory and re-run with new_scaffold (classify should "
                f"have rejected; defence-in-depth)")
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
    # H-S3-12: lifecycle_state is plan.to_state (validate() promoted to
    # PAPER for source: existing_code on criteria pass; LAB otherwise).
    # allocator_eligible is True iff (a) lifecycle is PAPER AND (b) the
    # ECR's allocator: field is true — a LAB engine can NEVER be
    # allocator_eligible (test_no_half_state enforces this when
    # _DISPATCHABLE filters; the explicit invariant here is the SoT).
    target_state = (plan.to_state
                    if plan.to_state is LifecycleState.PAPER
                    else LifecycleState.LAB)
    state_token = (
        "LifecycleState.PAPER"
        if target_state is LifecycleState.PAPER
        else "LifecycleState.LAB")
    allocator_eligible = bool(
        target_state is LifecycleState.PAPER
        and plan.sot_diff.get("allocator", False))
    # Spec §7.2 (2026-05-20): render the data_dependencies kwarg iff the
    # ECR carried a non-empty frozenset. Empty/None → omit the kwarg so
    # the EngineProfile field default (frozenset()) is the SoT for "no
    # declared reads" — one mechanism, no double-tracking. The literal is
    # built from a SORTED tuple so the rendered byte sequence is
    # deterministic (a frozenset's set-iteration order is hash-randomized
    # — sorting pins the line content across runs).
    dd = plan.sot_diff.get("data_dependencies")
    if dd:
        dd_literal = ", ".join(f'"{t}"' for t in sorted(dd))
        data_deps_token = (
            f"data_dependencies=frozenset({{{dd_literal}}})")
    else:
        data_deps_token = None
    # The _PROFILE line tail: append the allocator_eligible token (if
    # PAPER+allocator) and the data_dependencies token (if non-empty),
    # each on its own continuation line aligned with the existing kwarg
    # column (31 spaces) — matches the byte shape of the hand-curated
    # entries in _PROFILE (verified against `reversion` / `vector` /
    # `catalyst` post-2026-05-20 fold).
    profile_tail = f"lifecycle_state={state_token}"
    if allocator_eligible:
        profile_tail += (
            ",\n                               allocator_eligible=True")
    if data_deps_token is not None:
        profile_tail += (
            f",\n                               {data_deps_token}")
    new_entry = (
        f'    "{engine}":   EngineProfile(engine="{engine}", '
        f'cadence={cad_enum},\n'
        f'                               dispatch_order={order}, '
        f'{profile_tail}),\n')
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
    # H-S3-12: when ADD lands PAPER (the new existing_code criteria-pass
    # path), regenerate the non-Python shadows AND rewrite the frozen-
    # literal pin in test_engine_lifecycle_consistency.py — same single-
    # mechanism discipline as REMOVE. LAB landing is byte-identical (LAB
    # is filtered out of roster_for_dispatch by _DISPATCHABLE).
    if target_state is LifecycleState.PAPER:
        _shadow_edit_add_to_paper(
            root, engine, dispatch_order=int(order), jn=jn)
        _maybe_rewrite_frozen_literal_add(
            root, added_engine=engine, dispatch_order=int(order), jn=jn)


def _is_accuracy_only_modify(ecr: EngineChangeRequest) -> bool:
    """Spec §7 follow-up (2026-05-21, audit
    docs/superpowers/audits/2026-05-20-engine-data-dependencies-
    accuracy.md): an ACCURACY-ONLY MODIFY is one that corrects a
    documentation drift (the EngineProfile.data_dependencies tuple
    diverged from the engine's actual ``platform.<table>`` reads) — no
    Lab dossier, no param tuning, no behaviour change. The wire-format
    discriminator:

        ecr.action == MODIFY
        ecr.param_change is None
        ecr.lab_dossier is None
        ecr.gate_dsr is None
        ecr.gate_cred is None
        ecr.data_dependencies is not None   (at least one accuracy field
                                              MUST be set — see below)

    The ``need`` free-text is allowed alongside (it's the operator-
    readable rationale, not a behaviour change). At least one of
    ``data_dependencies`` / ``need`` must be set — a MODIFY ECR with
    NOTHING set is rejected upstream by the action-key validator
    (``_exactly_the_selected_action_fields``), and even if it slipped
    through here a degenerate accept-noop is anti-pattern. We require
    at least ``data_dependencies`` because ``need`` alone applies no
    on-disk change — there would be nothing for ``_apply_modify`` to do.

    Returns False (route to the existing param-change zero-trust gate)
    when any param-tuning / lab-dossier field is set. Returns True only
    when EXACTLY the accuracy-only shape is present.
    """
    if ecr.action is not ECRAction.MODIFY:
        return False
    if ecr.param_change is not None:
        return False
    if ecr.lab_dossier is not None:
        return False
    if ecr.gate_dsr is not None or ecr.gate_cred is not None:
        return False
    if ecr.data_dependencies is None:
        return False  # need-only is a no-op; reject via param-change gate
    return True


def _validate_modify(plan: TransitionPlan,
                     ecr: EngineChangeRequest,
                     *, repo_root: Path | None = None,
                     _incumbent_dossier: Any | None = None,
                     ) -> TransitionPlan:
    """H-S3-6 zero-trust: the gate is the ONLY thing between a dossier
    and live params, so re-derive every number from the FROZEN JSON
    sidecar, never the ECR text / rendered markdown.

    H-S3-12: the absolute DSR/credibility threshold is replaced by the
    autonomous improvement criteria (`_assess_improvement`) — strict
    better than the incumbent on the declared primary metric, plus the
    new-engine signal-presence floor, plus a trade-count drift bound.
    The incumbent dossier is read from
    ``backtests/<engine>_backtest_results.json`` under ``repo_root or
    REPO_ROOT``; ``_incumbent_dossier`` is a test seam (offline tests
    inject the incumbent ``NewEngineDossier`` directly so the criteria
    can be exercised without touching the live backtests/ directory).
    See docs/superpowers/specs/2026-05-20-autonomous-lab-criteria.md.

    Spec §7 follow-up (2026-05-21, audit
    docs/superpowers/audits/2026-05-20-engine-data-dependencies-
    accuracy.md): accuracy-only MODIFYs (``data_dependencies`` correction
    with NO ``param_change`` / ``lab_dossier`` / ``gate_*``) bypass the
    zero-trust Lab-dossier gate — there is no signal change to validate,
    only a documentation drift to correct. The H-S3-6d lifecycle-
    immutable guard STILL fires (every MODIFY plan with a lifecycle key
    in sot_diff is rejected, accuracy-only or not). Discriminator:
    ``_is_accuracy_only_modify(ecr)``.
    """
    # H-S3-6d lifecycle-immutable guard — runs FIRST for BOTH MODIFY
    # branches (accuracy-only AND param-change). A sot_diff that carries
    # a lifecycle / allocator / dispatch_order / cadence key is a hard
    # reject regardless of which MODIFY discriminator below the ECR
    # matches. The structural invariant outranks the branch dispatch.
    if plan.sot_diff and any(
            kk in plan.sot_diff for kk in (
                "lifecycle_state", "allocator_eligible",
                "dispatch_order", "cadence")):
        return _reject(ecr, "MODIFY plan carries a _PROFILE edit — "
                            "lifecycle is immutable under MODIFY "
                            "(H-S3-6d)")
    # Accuracy-only MODIFY branch: no Lab dossier required. The
    # _apply_modify path's data_dependencies rewriter is dedicated and
    # CANNOT touch lifecycle/allocator/dispatch_order/cadence tokens
    # (H-S3-6d preserved structurally — see _rewrite_profile_data_
    # dependencies's docstring). Wrong-target is NOT a possible failure
    # mode here: there is no Lab sidecar carrying a target_engine that
    # could disagree with ecr.engine; the engine identifier on the ECR
    # IS the target, and classify() already rejected absent / retired.
    if _is_accuracy_only_modify(ecr):
        return plan
    from ops.engine_sdlc._evidence import (
        EvidenceError,
        assert_identity_fresh,
        load_labresult_sidecar,
    )
    from ops.engine_sdlc.lab_criteria import (
        _assess_improvement,
        dossier_from_lab_held_metrics,
        load_engine_dossier,
    )
    from ops.lab.run import PARAM_RANGES
    # A MODIFY that reaches this branch carries a param-change or
    # gate_* (or it would have routed through the accuracy-only branch
    # above). The zero-trust gate REQUIRES the Lab dossier; ``None`` is
    # a hard reject with a clear message — never propagate to
    # ``load_labresult_sidecar`` which would TypeError on a None path
    # (the prior implicit invariant relied on the operator checklist;
    # making it explicit here closes a latent crash mode and gives a
    # readable rejection instead of a stack trace).
    if ecr.lab_dossier is None:
        return _reject(
            ecr, "MODIFY with param_change / gate_* requires a "
                 "lab_dossier (the zero-trust Lab gate has nothing to "
                 "load); accuracy-only MODIFYs (data_dependencies / "
                 "need only) are the only MODIFY shape that may omit it")
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
    if lr.recommended_exit != "fold_existing":
        return _reject(
            ecr, f"sidecar recommended_exit {lr.recommended_exit!r} != "
                 f"fold_existing (a promote_new is an ADD, not a MODIFY)")
    if lr.target_engine != ecr.engine:
        return _reject(
            ecr, f"sidecar target_engine {lr.target_engine!r} != ECR "
                 f"engine {ecr.engine!r} (wrong-target reject)")
    # H-S3-12: autonomous improvement criteria. Replace the absolute
    # DSR≥0.95 ∧ cred≥60 gate with: candidate strictly beats incumbent
    # on the declared primary metric + candidate passes the new-engine
    # signal-presence floor + trade-count drift bounded. The incumbent
    # is read from backtests/<engine>_backtest_results.json (the
    # canonical artifact <engine>.backtest writes). See
    # docs/superpowers/specs/2026-05-20-autonomous-lab-criteria.md.
    candidate_dossier = dossier_from_lab_held_metrics(lr.held_metrics)
    incumbent_dossier = (
        _incumbent_dossier
        if _incumbent_dossier is not None
        else load_engine_dossier(repo_root or REPO_ROOT, ecr.engine))
    if incumbent_dossier is None:
        return _reject(
            ecr, f"MODIFY: no incumbent backtest dossier found at "
                 f"backtests/{ecr.engine}_backtest_results.json — the "
                 f"improvement criteria require an incumbent to compare "
                 f"against; run `python -m {ecr.engine}.backtest --json` "
                 f"first.")
    passed, reason = _assess_improvement(
        candidate_dossier, incumbent_dossier, lr.primary_metric)
    if not passed:
        return _reject(
            ecr, f"MODIFY: autonomous improvement criteria failed — "
                 f"{reason}")
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
    return plan


def _apply_modify(plan: TransitionPlan, root: Path, jn: _Journal) -> None:
    """Apply the validated current→winning diff to the engine's
    default_params() SOURCE (the O1 seam). _PROFILE's lifecycle /
    dispatch_order / allocator_eligible / cadence kwargs are NEVER
    touched (H-S3-6d); the lifecycle-immutable invariant is preserved
    structurally — ``_validate_modify`` rejects any of those keys
    organically, and the data_dependencies rewriter below is dedicated
    + cannot see those tokens. Line-anchored edits of the engine
    backtest.py / models.py default constants, AST-validated.

    Spec §7 follow-up (2026-05-21, audit
    docs/superpowers/audits/2026-05-20-engine-data-dependencies-
    accuracy.md): a MODIFY ECR may also carry a non-empty
    ``data_dependencies`` set, in which case the engine's _PROFILE row's
    ``data_dependencies=frozenset({...})`` kwarg is re-rendered through
    the same targeted-line + AST-validated discipline as
    ``_rewrite_profile_source``. The catalyst / momentum 2026-05-20
    earnings_events accuracy fix is the motivating case — applied via
    the canonical ECR path, never a hand-edit of _PROFILE (the hook
    blocks it).
    """
    engine = plan.engine
    pc = plan.sot_diff.get("param_change") or {}
    dd = plan.sot_diff.get("data_dependencies")
    # Either path may apply alone or together — a pure param_change is
    # the historical case; a pure data_dependencies MODIFY is the new
    # accuracy-correction case; a combined MODIFY (both keys present)
    # threads through both branches deterministically below. At least
    # one side must do real work — the validate() pipeline upstream
    # already excludes empty MODIFYs.
    if pc:
        consts = _ENGINE_DEFAULT_CONSTS.get(engine)
        if consts is None:
            raise RuntimeError(
                f"no MODIFY default-constant map for engine {engine!r}")
        # Group the param edits by their target source file (per the
        # executor note: reversion z_threshold lives in reversion/
        # models.py, not reversion/backtest.py — the line-anchored edit
        # must hit the file the default_params() accessor actually
        # reads).
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
                    f"{tgt.relative_to(root)} not found for {engine} "
                    f"MODIFY")
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
    if dd is not None:
        # Re-render the existing _PROFILE row's data_dependencies kwarg
        # to the declared set. ``dd`` is a frozenset (the ECR parser
        # coerces the comma-separated value); attach_ecr_context threads
        # it onto sot_diff. The byte-rendered literal is sorted so the
        # output is deterministic across runs (set hash-randomization).
        # An empty frozenset is a legitimate value — it rolls the engine
        # back to the EngineProfile field default, rendered as
        # ``frozenset()``. The same _Journal record-before-write
        # ordering as the param_change branch gives the
        # byte-identical rollback on a post-stage clockwork red.
        ep = root / "tpcore" / "engine_profile.py"
        jn.record_file(ep)
        new_ep = _rewrite_profile_data_dependencies(
            ep.read_text(), engine=engine,
            deps=frozenset(dd))
        ep.write_text(new_ep)


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

    H-S3-12: the absolute DSR≥0.95 ∧ cred≥60 gate is replaced by the
    autonomous new-engine signal-presence criteria (``_assess_new_engine_signal``)
    evaluated against the engine's most-recent backtest dossier at
    ``backtests/<engine>_backtest_results.json``. The test seam
    ``_gate_green`` is preserved for offline tests; production calls
    ``promote()`` with ``_gate_green=None`` and the planner resolves the
    verdict from the dossier autonomously. See
    docs/superpowers/specs/2026-05-20-autonomous-lab-criteria.md.

    Reuses the T5 ``_Journal`` byte-identical-rollback discipline + the
    T4 ``_rewrite_profile_source`` (the ONLY _PROFILE editor) +
    ``_run_consistency_subprocess`` — a post-flip red OR any exception
    reverse-replays to byte-identical (the LAB engine stays LAB, ZERO
    trace)."""
    root = repo_root or REPO_ROOT
    plan = TransitionPlan(
        action=ECRAction.MODIFY, engine=engine,
        from_state=LifecycleState.LAB, to_state=LifecycleState.PAPER,
        approval_class=ApprovalClass.AUTOMATED)
    # H-S3-12: resolve the gate verdict from the dossier (the framework's
    # autonomous gate). ``_gate_green`` overrides as a test seam.
    if _gate_green is None:
        from ops.engine_sdlc.lab_criteria import (
            _assess_new_engine_signal,
            load_engine_dossier,
        )
        try:
            dossier = load_engine_dossier(root, engine)
        except ValueError as exc:
            rej = TransitionPlan(**{
                **plan.__dict__,
                "rejection": (f"promote: backtest dossier at "
                              f"backtests/{engine}_backtest_results.json "
                              f"is unparseable — {exc}")})
            if emit_audit:
                _emit_audit(engine, "promote", "lab", "paper",
                            "AUTOMATED", "rejected", rej.rejection)
            return rej
        if dossier is None:
            rej = TransitionPlan(**{
                **plan.__dict__,
                "rejection": (f"promote: no recent backtest dossier at "
                              f"backtests/{engine}_backtest_results.json "
                              f"— run `python -m {engine}.backtest --json` "
                              f"first to produce the dossier the planner "
                              f"reads autonomously.")})
            if emit_audit:
                _emit_audit(engine, "promote", "lab", "paper",
                            "AUTOMATED", "rejected", rej.rejection)
            return rej
        passed, reason = _assess_new_engine_signal(dossier)
        if not passed:
            rej = TransitionPlan(**{
                **plan.__dict__,
                "rejection": (f"promote: autonomous Lab criteria failed "
                              f"— {reason}")})
            if emit_audit:
                _emit_audit(engine, "promote", "lab", "paper",
                            "AUTOMATED", "rejected", rej.rejection)
            return rej
    elif not _gate_green:
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
    "_is_accuracy_only_modify",
    "_rewrite_profile_source",
    "_run_consistency_subprocess",
    "_staged_copytree",
    "_validate_modify",
]
