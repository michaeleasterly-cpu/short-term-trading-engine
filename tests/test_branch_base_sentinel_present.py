"""Branch-base-sentinel workflow presence + invariant test.

Pins the load-bearing properties of
``.github/workflows/branch-base-sentinel.yml`` — the PR-time check
that a PR's base branch is an ancestor of HEAD.

The workflow was introduced 2026-06-04 as the durable backstop for
the wrong-base PR failure mode the morning audit named in §13 #4
and that bit PR #458's authoring live (an EnterWorktree call
inherited the operator's deferred-arc checkout instead of
origin/main).

Presence + smart-substring checks only — the behavior test is the
live PR run. A future revision that removes one of the load-bearing
substrings reds CI so the operator gets a chance to re-confirm the
invariant survives the rewrite.

Per ``.claude/rules/tests-and-ci.md``: this test runs no ``git``,
``gh``, or DB access — pure filesystem reads of the tracked
workflow.
"""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_WORKFLOW = (
    _REPO / ".github" / "workflows" / "branch-base-sentinel.yml"
)


def _text() -> str:
    assert _WORKFLOW.is_file(), f"missing workflow: {_WORKFLOW}"
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert text.strip(), f"workflow is empty: {_WORKFLOW}"
    return text


def test_workflow_present_and_non_empty() -> None:
    _text()


def test_workflow_triggers_on_pull_request() -> None:
    """The workflow must run on every PR open/sync/reopen/ready."""
    text = _text()
    assert "pull_request:" in text, (
        "workflow must trigger on pull_request"
    )
    for event_type in ("opened", "synchronize", "reopened", "ready_for_review"):
        assert event_type in text, (
            f"workflow must include {event_type!r} in `pull_request.types` so "
            "the check fires on every legitimate PR-author event"
        )


def test_workflow_permissions_are_read_only() -> None:
    """The sentinel is read-only — it must never grant write."""
    text = _text()
    assert "contents: read" in text, (
        "workflow must declare contents: read"
    )
    assert "contents: write" not in text, (
        "workflow MUST NOT declare contents: write — branch-base check "
        "is read-only by design"
    )


def test_workflow_uses_pinned_action_version() -> None:
    """actions/checkout must be pinned to a major-version tag, never @main."""
    text = _text()
    assert "actions/checkout@" in text, (
        "workflow must use actions/checkout"
    )
    assert "actions/checkout@main" not in text, (
        "actions/checkout MUST NOT be pinned to @main (would silently shift)"
    )


def test_workflow_does_the_ancestor_check() -> None:
    """The merge-base ancestor check is the load-bearing assertion —
    if it's missing or weakened, the sentinel no longer enforces the
    invariant."""
    text = _text()
    assert "git merge-base --is-ancestor" in text, (
        "workflow must call `git merge-base --is-ancestor` — that's the "
        "canonical 'base is ancestor of HEAD' check"
    )
    assert 'pull_request.base.ref' in text, (
        "workflow must reference github.event.pull_request.base.ref so "
        "the check uses the PR's declared base, not a hardcoded branch"
    )


def test_workflow_emits_actionable_fix_instructions() -> None:
    """When the check fails, the failure message must tell the
    operator how to fix it (rebase + force-push) so a future operator
    isn't left guessing."""
    text = _text()
    assert "rebase" in text, (
        "workflow failure message must name `git rebase` as the fix"
    )
    assert "force-with-lease" in text, (
        "workflow failure message must name `--force-with-lease` as "
        "the canonical safe-force-push form (not bare `--force`)"
    )
    assert "worktree.baseRef" in text, (
        "workflow failure message must point at .claude/settings.json "
        "worktree.baseRef so subagent-authored PRs get debugged at the "
        "right layer"
    )


def test_workflow_has_full_history_checkout() -> None:
    """fetch-depth: 0 is required so merge-base can resolve any
    common ancestor — a shallow checkout would silently fail the
    check on long-lived branches."""
    text = _text()
    assert "fetch-depth: 0" in text, (
        "workflow must use fetch-depth: 0 — a shallow checkout would "
        "miss historical commits needed by merge-base"
    )


def test_workflow_concurrency_cancels_in_progress() -> None:
    """Match the ci.yml concurrency discipline — older runs on the
    same PR ref get canceled when a new commit lands."""
    text = _text()
    assert "concurrency:" in text, (
        "workflow should declare a concurrency group to avoid pileups"
    )
    assert "cancel-in-progress: true" in text, (
        "concurrency.cancel-in-progress: true required"
    )


def test_workflow_references_morning_audit() -> None:
    """The workflow must point at the audit doc that motivated it so
    a future maintainer can find the design rationale."""
    text = _text()
    assert "2026-06-03-claude-code-workflow-controls.md" in text, (
        "workflow comments must reference the morning audit so the "
        "design rationale (and §13 #4) is one click away"
    )
