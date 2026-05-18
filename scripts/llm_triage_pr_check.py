"""Thin CI entrypoint for the LLM-triage deterministic fence.

Checks two properties of a diff vs the base branch:
1. hard_denied_paths  — body-path gate (auto-fail if any body file touched)
2. provenance_violations — brain-gate (registry-only changes must be purely
   additive bindings to already-proven stages/params)

Usage (CI):
    python scripts/llm_triage_pr_check.py

Env vars:
    GITHUB_BASE_REF  — base branch name (default: main)

Fail-closed: any internal error exits 1.  A fence that silently passes on
error defeats the purpose.
"""
from __future__ import annotations

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


def _load_specs_head() -> dict[str, dict[str, dict]]:
    """Load both registries from the current HEAD (in-process import)."""
    out: dict[str, dict[str, dict]] = {}
    for name, snippet in _REGISTRY_SNIPPETS:
        raw = _run([sys.executable, "-c", snippet])
        out[name] = json.loads(raw)
    return out


def _load_specs_base(base_ref: str) -> dict[str, dict[str, dict]]:
    """Load both registries from the base branch via a git worktree."""
    with tempfile.TemporaryDirectory(prefix="llm_triage_base_") as tmpdir:
        wt_path = os.path.join(tmpdir, "base_wt")
        _run(["git", "worktree", "add", "--detach", wt_path,
              f"origin/{base_ref}"])
        try:
            out: dict[str, dict[str, dict]] = {}
            for name, snippet in _REGISTRY_SNIPPETS:
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    base_ref = os.environ.get("GITHUB_BASE_REF", "main")
    failed = False

    # ------------------------------------------------------------------
    # 1. Hard-denied path check
    # ------------------------------------------------------------------
    try:
        from tpcore.llm_data_triage.fence import hard_denied_paths
        paths = _diff_paths(base_ref)
        denied = hard_denied_paths(paths)
        if denied:
            print("FENCE FAIL — hard-denied paths touched:")
            for p in denied:
                print(f"  {p}")
            failed = True
        else:
            print(f"hard_denied_paths: OK ({len(paths)} path(s) checked)")
    except Exception as exc:
        print(f"FENCE ERROR in hard_denied_paths: {exc}")
        sys.exit(1)

    # ------------------------------------------------------------------
    # 2. Provenance check
    # ------------------------------------------------------------------
    try:
        from tpcore.llm_data_triage.fence import provenance_violations

        head_specs = _load_specs_head()
        base_specs = _load_specs_base(base_ref)

        all_violations: list[str] = []
        for name in head_specs:
            baseline = base_specs.get(name, {})
            head = head_specs[name]
            baseline_stages = {v["stage"] for v in baseline.values()}
            viols = provenance_violations(baseline, head, baseline_stages)
            for viol in viols:
                all_violations.append(f"[{name}] {viol}")

        if all_violations:
            print("FENCE FAIL — provenance violations:")
            for viol in all_violations:
                print(f"  {viol}")
            failed = True
        else:
            print("provenance_violations: OK")
    except Exception as exc:
        print(f"FENCE ERROR in provenance_violations: {exc}")
        sys.exit(1)

    if failed:
        sys.exit(1)

    print("fence: all checks passed")


if __name__ == "__main__":
    main()
