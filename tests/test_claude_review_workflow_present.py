"""Anti-rot sentinel for the heavy-lane Claude review workflow.

Mirrors the ``test_claude_{rules,skills,agents,hooks}_present.py``
precedent: presence + load-bearing properties, NOT behaviour. The
behavior test is a live PR run.

The workflow is review/comment-only first-pass automation modeled on
the Anthropic ``claude-code-action`` examples and is described in
``.github/workflows/claude-review-heavy-lane.yml``. This sentinel
asserts the workflow keeps its review-only invariants intact across
future edits.

Authoritative external:
  * https://github.com/anthropics/claude-code-action
"""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_WORKFLOW = (
    _REPO / ".github" / "workflows" / "claude-review-heavy-lane.yml"
)


def _text() -> str:
    assert _WORKFLOW.is_file(), f"missing workflow: {_WORKFLOW}"
    return _WORKFLOW.read_text(encoding="utf-8")


def test_workflow_file_present() -> None:
    assert _WORKFLOW.is_file(), f"missing workflow: {_WORKFLOW}"
    assert _WORKFLOW.read_text(encoding="utf-8").strip(), "workflow is empty"


def test_workflow_triggers_on_pull_request() -> None:
    src = _text()
    assert "pull_request:" in src, "workflow must trigger on pull_request"


def test_workflow_has_heavy_lane_path_filters() -> None:
    """The workflow's ``paths:`` filter must include the canonical
    heavy-lane subset (kept in sync with
    ``.claude/rules/heavy-lane.md`` by
    ``scripts/check_manifests.py``)."""
    src = _text()
    canonical = (
        "tpcore/risk/**",
        "tpcore/selfheal/**",
        "tpcore/auditheal/**",
        "tpcore/quality/validation/**",
        "ops/engine_service.py",
        "ops/engine_sdlc/**",
        "platform/migrations/**",
        "tpcore/engine_profile.py",
        "tpcore/providers.py",
    )
    for path in canonical:
        # Match either quoted or unquoted form (the workflow uses
        # quoted; we accept both for forward compatibility).
        assert (
            f'"{path}"' in src or f"'{path}'" in src or f"- {path}" in src
        ), f"workflow paths filter missing canonical heavy-lane entry: {path}"


def test_workflow_references_anthropic_api_key_secret() -> None:
    src = _text()
    assert "ANTHROPIC_API_KEY" in src, (
        "workflow must read secrets.ANTHROPIC_API_KEY"
    )
    # And ensure it's read from secrets, not hardcoded.
    assert "secrets.ANTHROPIC_API_KEY" in src or (
        "${{ secrets.ANTHROPIC_API_KEY }}" in src
    ), "ANTHROPIC_API_KEY must come from secrets, not a literal"


def test_workflow_does_not_grant_contents_write() -> None:
    """Review-only invariant: the action MUST NOT have ``contents:
    write`` permission. If a future use case requires commits, it
    belongs in a separate workflow with operator-explicit auth."""
    src = _text()
    assert "contents: write" not in src, (
        "review-only workflow must NOT grant contents: write — "
        "use a separate workflow with operator authorization if "
        "commits are needed"
    )


def test_workflow_describes_review_only_intent() -> None:
    """The workflow's prompt must explicitly describe its review-only
    intent + prohibit code changes / auto-fix / auto-merge — so a
    future edit can't relax the boundary silently."""
    src = _text()
    review_only_markers = ("review/comment-only", "review only", "REVIEW ONLY")
    assert any(marker in src for marker in review_only_markers), (
        "workflow prompt must say review-only somewhere"
    )
    for forbidden_keyword in (
        "auto-merge",
        "auto-fix",
    ):
        # The text MUST include the forbidden term as a NEGATION
        # ("do not auto-merge", "do NOT auto-fix"). If the term
        # appears without a negation nearby, that's a smell.
        if forbidden_keyword in src.lower():
            # Look for "not" or "no " or "NOT" within ~80 chars before
            # the term.
            idx = src.lower().find(forbidden_keyword)
            window = src[max(0, idx - 80):idx].lower()
            assert (
                " not " in window
                or " no " in window
                or "prohibit" in window
                or "must not" in window
            ), (
                f"workflow mentions {forbidden_keyword!r} without a "
                "negation — review-only intent is at risk"
            )


def test_workflow_uses_pinned_action_version() -> None:
    """The Anthropic action must be pinned to a major-version tag
    (``@v1``) or a SHA — NEVER ``@main`` (would silently shift)."""
    src = _text()
    assert "anthropics/claude-code-action" in src, (
        "workflow must use anthropics/claude-code-action"
    )
    assert "anthropics/claude-code-action@main" not in src, (
        "anthropics/claude-code-action MUST NOT be pinned to @main "
        "(would silently shift behavior)"
    )


def test_workflow_concurrency_cancel_in_progress() -> None:
    """Mirror the ci.yml concurrency discipline — older reviews on the
    same PR ref get canceled when a new commit lands."""
    src = _text()
    assert "concurrency:" in src, (
        "workflow should declare a concurrency group to avoid pileups"
    )
    assert "cancel-in-progress: true" in src, (
        "concurrency.cancel-in-progress: true required so newer "
        "commits supersede older review jobs"
    )
