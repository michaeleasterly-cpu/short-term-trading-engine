"""Phase D auto-promote — Task #25 §3.2 + §3.3.

Extends SP-G's ``emit_once`` with the autonomous-loop post-emission
steps: undraft → CI green wait → gate pass check → auto-merge → write
``LAB_FINDER_ACTION`` provenance rows.

This is a CALLER of ``emit_once`` (per spec §3.3: "Task #25 is a
caller of `emit_once`; it NEVER reimplements an SP-G function"). The
SP-G fence stack (ledger pre-check, EmittedSpec validate,
record_trial_spend, render, enforce_diff_scope, validate_no_gate_override,
gh pr create --draft) runs verbatim via emit_once. This module adds
the auto-promote layer on top.

Safety:
- Auto-merge ONLY on branches matching the finder-PR pattern
  (``task-25-finder/...`` per spec §10.3 ``test_finder_auto_merge_branch_pattern``).
- Auto-merge ONLY after CI green (statusCheckRollup.conclusion=="SUCCESS").
- NEVER calls ``gh pr merge`` outside the bounded ``_auto_merge_pr`` helper.
- ALL gh commands routed through the pluggable ``pr_runner`` (test seam).
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

log = structlog.get_logger(__name__)

# Finder-branch pattern fence (spec §10.3 test_finder_auto_merge_branch_pattern).
_FINDER_BRANCH_PATTERN = re.compile(r"^task-25-finder/")

# Public PR-runner protocol — matches SP-G's existing signature.
PRRunner = Callable[..., tuple[int, str, str]]


class AutoPromoteError(RuntimeError):
    """Auto-promote attempt failed; finder-action provenance row carries detail."""


class BranchPatternViolation(AutoPromoteError):
    """Auto-merge attempted on a branch outside the finder-PR pattern."""


def _is_finder_branch(branch_name: str) -> bool:
    """Branch-pattern fence per spec §10.3."""
    return bool(_FINDER_BRANCH_PATTERN.match(branch_name))


def _extract_pr_branch(pr_url: str, pr_view_json: dict[str, Any]) -> str:
    """Read headRefName from a `gh pr view --json` result."""
    return str(pr_view_json.get("headRefName", ""))


def _is_ci_pass(pr_view_json: dict[str, Any]) -> tuple[bool, str]:
    """Return (passed, reason) from the statusCheckRollup of `gh pr view --json`.

    Pass iff EVERY non-skipped check is SUCCESS.
    """
    rollup = pr_view_json.get("statusCheckRollup") or []
    if not rollup:
        return False, "no_checks_yet"
    statuses = []
    for check in rollup:
        conclusion = check.get("conclusion", "")
        if conclusion == "SKIPPED":
            continue
        statuses.append(conclusion)
    if not statuses:
        return False, "all_skipped"
    if any(s != "SUCCESS" for s in statuses):
        failing = [s for s in statuses if s != "SUCCESS"]
        return False, f"check_not_passing: {failing[0]}"
    return True, "all_pass"


async def auto_promote_pr(
    pool: asyncpg.Pool,
    *,
    pr_url: str,
    run_id: str,
    pr_runner: PRRunner,
) -> dict[str, Any]:
    """Phase D — undraft + auto-merge a finder-emitted draft PR.

    Steps:
    D1. ``gh pr view`` to read branch + status checks.
    D2. Branch-pattern fence: must match ``task-25-finder/...``.
    D3. CI-green check via statusCheckRollup.
    D4. ``gh pr ready`` (undraft).
    D5. ``gh pr merge --auto --squash``.
    D6. Write LAB_FINDER_ACTION(action='merge', triggered_by='ci_green').

    Returns:
        Dict with action, triggered_by, pr_url, branch, ci_status.

    Raises:
        BranchPatternViolation: if PR branch does not match the finder pattern.
        AutoPromoteError: if any gh command fails or CI is not green.
    """
    # D1: read PR state
    code, stdout, stderr = pr_runner(
        ["gh", "pr", "view", pr_url, "--json", "headRefName,statusCheckRollup,state"],
    )
    if code != 0:
        raise AutoPromoteError(f"gh pr view failed: {stderr[:200]}")
    import json
    pr_state = json.loads(stdout) if stdout else {}

    branch = _extract_pr_branch(pr_url, pr_state)

    # D2: branch fence
    if not _is_finder_branch(branch):
        raise BranchPatternViolation(
            f"PR branch '{branch}' does not match task-25-finder/ pattern"
        )

    # D3: CI-green check
    ci_pass, ci_reason = _is_ci_pass(pr_state)
    from tpcore.lab.llm_finder.run_writer import record_finder_action
    if not ci_pass:
        await record_finder_action(
            pool,
            run_id=run_id,
            action="undraft_skip",
            triggered_by="ci_failed",
            extra={"pr_url": pr_url, "ci_reason": ci_reason, "branch": branch},
        )
        raise AutoPromoteError(f"CI not green: {ci_reason}")

    # D4: undraft (gh pr ready)
    code, stdout, stderr = pr_runner(["gh", "pr", "ready", pr_url])
    if code != 0:
        raise AutoPromoteError(f"gh pr ready failed: {stderr[:200]}")

    # D5: merge (gh pr merge --auto --squash)
    code, stdout, stderr = pr_runner(
        ["gh", "pr", "merge", pr_url, "--auto", "--squash"],
    )
    if code != 0:
        raise AutoPromoteError(f"gh pr merge failed: {stderr[:200]}")

    # D6: provenance
    await record_finder_action(
        pool,
        run_id=run_id,
        action="merge",
        triggered_by="ci_green",
        extra={"pr_url": pr_url, "branch": branch},
    )

    log.info(
        "auto_promote.merged",
        pr_url=pr_url,
        branch=branch,
        run_id=run_id,
    )
    return {
        "action": "merge",
        "triggered_by": "ci_green",
        "pr_url": pr_url,
        "branch": branch,
        "ci_status": "pass",
    }


__all__ = [
    "AutoPromoteError",
    "BranchPatternViolation",
    "PRRunner",
    "_is_ci_pass",
    "_is_finder_branch",
    "auto_promote_pr",
]
