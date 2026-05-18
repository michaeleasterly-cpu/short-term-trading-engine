"""Thin CI entrypoint for the LLM-triage deterministic fence.

Checks two properties of a diff vs the base branch:
1. hard_denied_paths  — body-path gate (auto-fail if any body file touched)
2. provenance_violations — brain-gate (registry-only changes must be purely
   additive bindings to already-proven stages/params)

Lane-agnostic by ``--lane`` (Epic E Phase 3.2): the SAME #63-hardened
base-loader + the SAME shared pure fence evaluator serve BOTH lanes —
zero clone (one fence object is a safety asset; two could silently
diverge):

* ``--lane data`` (default — byte-identical to the shipped #187
  behaviour): baseline = the data-lane HealSpec/RemediationSpec
  registries; hard-denied = the data protected-path set.
* ``--lane engine``: baseline = ``ops.engine_ladder.DISPOSITION_POLICIES``
  normalised into the shared spec-dict (the disposition *verb* in the
  ``stage`` slot, ``baseline_stages`` = the existing
  ``EngineEscalationDisposition`` values); hard-denied = the engine
  protected-path set (the engine deterministic-mechanism files + the
  shared protected paths) — via ``tpcore.engine_llm_triage.fence``.

Usage (CI):
    python scripts/llm_triage_pr_check.py            # data lane (default)
    python scripts/llm_triage_pr_check.py --lane engine

Env vars:
    GITHUB_BASE_REF  — base branch name (default: main)

Fail-closed: any internal error exits 1.  A fence that silently passes on
error defeats the purpose. NEVER references any LLM API key / secret —
the LLM's self-judgement gates nothing; these artifact properties do.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent


def _run(cmd: list[str], *, check: bool = True, capture: bool = True) -> str:
    result = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        check=check,
        cwd=str(_REPO_ROOT),
    )
    return result.stdout.strip() if capture else ""


def _diff_paths(base_ref: str) -> list[str]:
    raw = _run(["git", "diff", "--name-only", f"origin/{base_ref}...HEAD"])
    return [p for p in raw.splitlines() if p]


# ---------------------------------------------------------------------------
# Spec normalisation
# ---------------------------------------------------------------------------

_SELFHEAL_IMPORT = (
    "from tpcore.selfheal.registry import HEAL_SPECS; "
    "import json; "
    "print(json.dumps({"
    "k: {'stage': v.stage, 'params': dict(v.params), "
    "    'act': v.healable, 'max_attempts': v.max_attempts} "
    "for k, v in HEAL_SPECS.items()}))"
)

_AUDITHEAL_IMPORT = (
    "from tpcore.auditheal.registry import REMEDIATION_SPECS; "
    "import json; "
    "print(json.dumps({"
    "k: {'stage': v.stage, 'params': dict(v.params), "
    "    'act': v.remediable, 'max_attempts': v.max_attempts} "
    "for k, v in REMEDIATION_SPECS.items()}))"
)

_REGISTRY_SNIPPETS = [
    ("selfheal", _SELFHEAL_IMPORT),
    ("auditheal", _AUDITHEAL_IMPORT),
]

# Engine lane (Epic E Phase 3.2): the engine lane has NO
# HealSpec/RemediationSpec set (confirmed by reading — spec §3/§11);
# its sole declarative SoT is `ops.engine_ladder.DISPOSITION_POLICIES`.
# Normalise each policy into the SHARED spec-dict shape so the SAME
# `provenance_violations` evaluator gates it with zero clone: the
# disposition VERB goes in the `stage` slot, `params` is always {}, the
# binding is `act=True`, `max_attempts=0`. `baseline_stages` (computed
# in main) = the set of existing disposition verbs ⇒ a new policy is
# allowed iff it is additive AND binds an ALREADY-EXISTING
# EngineEscalationDisposition value (never a new member / edited / removed
# policy — exactly spec §3/§4).
_ENGINE_DISPOSITION_IMPORT = (
    "from ops.engine_ladder import DISPOSITION_POLICIES; "
    "import json; "
    "print(json.dumps({"
    "k: {'stage': v.default.value, 'params': {}, "
    "    'act': True, 'max_attempts': 0} "
    "for k, v in DISPOSITION_POLICIES.items()}))"
)

_ENGINE_REGISTRY_SNIPPETS = [
    ("disposition_policies", _ENGINE_DISPOSITION_IMPORT),
]

_LANE_SNIPPETS = {
    "data": _REGISTRY_SNIPPETS,
    "engine": _ENGINE_REGISTRY_SNIPPETS,
}


def _load_specs_head(
    snippets: list[tuple[str, str]] = _REGISTRY_SNIPPETS,
) -> dict[str, dict[str, dict]]:
    """Load the lane's registries from the current HEAD (in-process
    import). ``snippets`` defaults to the DATA-lane set so existing
    callers/tests are byte-unchanged."""
    out: dict[str, dict[str, dict]] = {}
    for name, snippet in snippets:
        raw = _run([sys.executable, "-c", snippet])
        out[name] = json.loads(raw)
    return out


def _load_specs_base(
    base_ref: str,
    snippets: list[tuple[str, str]] = _REGISTRY_SNIPPETS,
) -> dict[str, dict[str, dict]]:
    """Load the lane's registries from the base branch via a git
    worktree. ``snippets`` defaults to the DATA-lane set (so the
    existing #187 callers + the cleanup test are byte-unchanged). The
    engine lane passes ``_ENGINE_REGISTRY_SNIPPETS`` — the SAME
    #63-hardened worktree-add + remove + defensive-prune-fallback +
    fail-loud host-guard path, zero clone."""
    try:
        with tempfile.TemporaryDirectory(prefix="llm_triage_base_") as tmpdir:
            wt_path = os.path.join(tmpdir, "base_wt")
            _run(["git", "worktree", "add", "--detach", wt_path,
                  f"origin/{base_ref}"])
            try:
                out: dict[str, dict[str, dict]] = {}
                for name, snippet in snippets:
                    raw = subprocess.run(
                        [sys.executable, "-c", snippet],
                        capture_output=True,
                        text=True,
                        check=True,
                        cwd=wt_path,
                        env={**os.environ, "PYTHONPATH": wt_path},
                    ).stdout.strip()
                    out[name] = json.loads(raw)
                return out
            finally:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", wt_path],
                    cwd=str(_REPO_ROOT),
                    capture_output=True,
                )
        # TemporaryDirectory has now been deleted (wt_path no longer exists
        # on disk).  Defensive best-effort prune: if ``git worktree remove``
        # above failed or was interrupted, a stale .git/worktrees/<name> admin
        # entry may survive.  ``git worktree prune`` reclaims entries whose
        # filesystem path is gone — this is the official-git-doc backed remedy
        # ("Removes stale administrative files for worktrees that have been
        # deleted").  Runs AFTER the TemporaryDirectory exits so the path is
        # definitively absent and git treats the entry as stale.  Must NEVER
        # raise — swallow all subprocess exceptions so the caller's
        # return/exception semantics are unchanged.
    finally:
        try:
            subprocess.run(  # noqa: S603 — fixed list-args, no shell, no user input
                ["git", "worktree", "prune"],
                cwd=str(_REPO_ROOT),
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass  # best-effort; a git absence/timeout must not mask the real return/exception


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _fence_callables(lane: str):
    """Return ``(hard_denied_fn, provenance_fn)`` for the lane. Both
    lanes resolve to the SAME shipped pure evaluator (zero clone): the
    data lane via `tpcore.llm_data_triage.fence` directly; the engine
    lane via the thin `tpcore.engine_llm_triage.fence` wrappers (which
    inject the engine denied-set DATA into that SAME function — no new
    fence logic). Imported lazily so a fence ERROR fails closed (exit 1)
    rather than crashing at module import."""
    if lane == "engine":
        from tpcore.engine_llm_triage.fence import (
            engine_hard_denied_paths,
            engine_provenance_violations,
        )

        def _prov(baseline, head, baseline_stages):
            return engine_provenance_violations(
                baseline, head, baseline_stages=baseline_stages
            )

        return engine_hard_denied_paths, _prov

    from tpcore.llm_data_triage.fence import (
        hard_denied_paths,
        provenance_violations,
    )

    return hard_denied_paths, provenance_violations


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python scripts/llm_triage_pr_check.py"
    )
    parser.add_argument(
        "--lane",
        choices=("data", "engine"),
        default="data",
        help="which lane's registries/denied-set to fence "
        "(default: data — byte-identical to the shipped #187 behaviour)",
    )
    args = parser.parse_args(argv)
    lane = args.lane
    snippets = _LANE_SNIPPETS[lane]

    base_ref = os.environ.get("GITHUB_BASE_REF", "main")
    failed = False

    hard_denied_fn, provenance_fn = _fence_callables(lane)

    # ------------------------------------------------------------------
    # 1. Hard-denied path check
    # ------------------------------------------------------------------
    try:
        paths = _diff_paths(base_ref)
        denied = hard_denied_fn(paths)
        if denied:
            print(f"FENCE FAIL [{lane}] — hard-denied paths touched:")
            for p in denied:
                print(f"  {p}")
            failed = True
        else:
            print(
                f"hard_denied_paths [{lane}]: OK "
                f"({len(paths)} path(s) checked)"
            )
    except Exception as exc:
        print(f"FENCE ERROR [{lane}] in hard_denied_paths: {exc}")
        sys.exit(1)

    # ------------------------------------------------------------------
    # 2. Provenance check
    # ------------------------------------------------------------------
    try:
        head_specs = _load_specs_head(snippets)
        base_specs = _load_specs_base(base_ref, snippets)

        all_violations: list[str] = []
        for name in head_specs:
            baseline = base_specs.get(name, {})
            head = head_specs[name]
            baseline_stages = {v["stage"] for v in baseline.values()}
            viols = provenance_fn(baseline, head, baseline_stages)
            for viol in viols:
                all_violations.append(f"[{name}] {viol}")

        if all_violations:
            print(f"FENCE FAIL [{lane}] — provenance violations:")
            for viol in all_violations:
                print(f"  {viol}")
            failed = True
        else:
            print(f"provenance_violations [{lane}]: OK")
    except Exception as exc:
        print(f"FENCE ERROR [{lane}] in provenance_violations: {exc}")
        sys.exit(1)

    if failed:
        sys.exit(1)

    print(f"fence [{lane}]: all checks passed")


if __name__ == "__main__":
    main()
