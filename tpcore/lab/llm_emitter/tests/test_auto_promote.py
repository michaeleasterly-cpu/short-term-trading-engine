"""Phase D auto-promote tests — Task #25 §10.3 + §10.2.

Covers:
- Branch-pattern fence (`task-25-finder/...` accepted; everything else rejected)
- CI-pass classifier (statusCheckRollup parsing)
- Happy path: undraft + merge + provenance row
- CI-not-green path: undraft_skip provenance + AutoPromoteError
- gh command failure → AutoPromoteError with bounded stderr
"""
from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.xdist_group("ops_shadow")


# ───────────────────────── FakePool for finder-action writes ─────────────


class _FakeConn:
    def __init__(self, sink: list[tuple[str, tuple[Any, ...]]]) -> None:
        self._sink = sink

    async def execute(self, sql: str, *args: Any) -> None:
        self._sink.append((sql, args))


class _AcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakePool:
    def __init__(self) -> None:
        self.sink: list[tuple[str, tuple[Any, ...]]] = []

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_FakeConn(self.sink))


# ───────────────────────── PR runner fake ─────────────────────────


def _make_pr_runner(*results: tuple[int, str, str]):
    """Return a callable that pops results in order; raises if exhausted."""
    iterator = iter(results)

    def runner(_argv: list[str], **_kwargs: Any) -> tuple[int, str, str]:
        try:
            return next(iterator)
        except StopIteration as exc:
            raise RuntimeError("pr_runner exhausted") from exc

    return runner


def _pr_view_json(
    branch: str = "task-25-finder/momentum-2026-05-21",
    checks: list[dict[str, Any]] | None = None,
) -> str:
    """A canned gh pr view --json result."""
    checks = checks if checks is not None else [
        {"name": "pytest", "conclusion": "SUCCESS"},
        {"name": "ruff", "conclusion": "SUCCESS"},
    ]
    return json.dumps({
        "headRefName": branch,
        "statusCheckRollup": checks,
        "state": "OPEN",
    })


# ───────────────────────── _is_finder_branch ─────────────────────────


def test_is_finder_branch_accepts_pattern() -> None:
    from tpcore.lab.llm_emitter.auto_promote import _is_finder_branch
    assert _is_finder_branch("task-25-finder/momentum-2026-05-21")
    assert _is_finder_branch("task-25-finder/anything")


def test_is_finder_branch_rejects_unmatching() -> None:
    from tpcore.lab.llm_emitter.auto_promote import _is_finder_branch
    assert not _is_finder_branch("main")
    assert not _is_finder_branch("feat/something-else")
    assert not _is_finder_branch("lab-emitter/foo")
    # Defense against substring smuggling.
    assert not _is_finder_branch("hax/task-25-finder/evil")


# ───────────────────────── _is_ci_pass ─────────────────────────


def test_is_ci_pass_all_success() -> None:
    from tpcore.lab.llm_emitter.auto_promote import _is_ci_pass
    pr = {"statusCheckRollup": [
        {"name": "pytest", "conclusion": "SUCCESS"},
        {"name": "ruff", "conclusion": "SUCCESS"},
    ]}
    passed, reason = _is_ci_pass(pr)
    assert passed
    assert reason == "all_pass"


def test_is_ci_pass_skipped_ignored() -> None:
    from tpcore.lab.llm_emitter.auto_promote import _is_ci_pass
    pr = {"statusCheckRollup": [
        {"name": "pytest", "conclusion": "SUCCESS"},
        {"name": "optional", "conclusion": "SKIPPED"},
    ]}
    passed, _ = _is_ci_pass(pr)
    assert passed


def test_is_ci_pass_failure_rejects() -> None:
    from tpcore.lab.llm_emitter.auto_promote import _is_ci_pass
    pr = {"statusCheckRollup": [
        {"name": "pytest", "conclusion": "SUCCESS"},
        {"name": "ruff", "conclusion": "FAILURE"},
    ]}
    passed, reason = _is_ci_pass(pr)
    assert not passed
    assert "FAILURE" in reason


def test_is_ci_pass_no_checks_yet_rejects() -> None:
    from tpcore.lab.llm_emitter.auto_promote import _is_ci_pass
    passed, reason = _is_ci_pass({"statusCheckRollup": []})
    assert not passed
    assert reason == "no_checks_yet"


def test_is_ci_pass_all_skipped_rejects() -> None:
    """Defense against an all-skipped statusCheck (would be a CI misconfig)."""
    from tpcore.lab.llm_emitter.auto_promote import _is_ci_pass
    pr = {"statusCheckRollup": [
        {"name": "x", "conclusion": "SKIPPED"},
        {"name": "y", "conclusion": "SKIPPED"},
    ]}
    passed, reason = _is_ci_pass(pr)
    assert not passed
    assert reason == "all_skipped"


# ───────────────────────── auto_promote_pr — happy path ─────────────────────


@pytest.mark.asyncio
async def test_auto_promote_pr_happy_path() -> None:
    from tpcore.lab.llm_emitter.auto_promote import auto_promote_pr

    pr_runner = _make_pr_runner(
        (0, _pr_view_json(), ""),          # D1: gh pr view
        (0, "", ""),                        # D4: gh pr ready
        (0, "", ""),                        # D5: gh pr merge
    )
    pool = _FakePool()
    result = await auto_promote_pr(
        pool,  # type: ignore[arg-type]
        pr_url="https://github.com/foo/bar/pull/42",
        run_id="run-1",
        pr_runner=pr_runner,
    )
    assert result["action"] == "merge"
    assert result["triggered_by"] == "ci_green"
    assert result["branch"] == "task-25-finder/momentum-2026-05-21"
    # Provenance row written.
    assert len(pool.sink) == 1
    sql, args = pool.sink[0]
    assert "LAB_FINDER_ACTION" in sql
    # Post-fix SQL: ($1 run_id, $2 message, $3 data jsonb) — payload is args[2].
    payload = json.loads(args[2])
    assert payload["action"] == "merge"
    assert payload["triggered_by"] == "ci_green"


# ───────────────────────── auto_promote_pr — branch fence ─────────────────


@pytest.mark.asyncio
async def test_auto_promote_pr_rejects_non_finder_branch() -> None:
    from tpcore.lab.llm_emitter.auto_promote import (
        BranchPatternViolation,
        auto_promote_pr,
    )

    pr_runner = _make_pr_runner(
        (0, _pr_view_json(branch="feat/some-other-feature"), ""),
    )
    pool = _FakePool()
    with pytest.raises(BranchPatternViolation, match="task-25-finder"):
        await auto_promote_pr(
            pool,  # type: ignore[arg-type]
            pr_url="https://github.com/foo/bar/pull/43",
            run_id="run-2",
            pr_runner=pr_runner,
        )
    # No merge attempted; no provenance.
    assert len(pool.sink) == 0


# ───────────────────────── auto_promote_pr — CI red ─────────────────────


@pytest.mark.asyncio
async def test_auto_promote_pr_skips_when_ci_red() -> None:
    from tpcore.lab.llm_emitter.auto_promote import (
        AutoPromoteError,
        auto_promote_pr,
    )

    pr_runner = _make_pr_runner(
        (0, _pr_view_json(checks=[
            {"name": "pytest", "conclusion": "FAILURE"},
        ]), ""),
    )
    pool = _FakePool()
    with pytest.raises(AutoPromoteError, match="CI not green"):
        await auto_promote_pr(
            pool,  # type: ignore[arg-type]
            pr_url="https://github.com/foo/bar/pull/44",
            run_id="run-3",
            pr_runner=pr_runner,
        )
    # undraft_skip provenance was written before the raise.
    assert len(pool.sink) == 1
    payload = json.loads(pool.sink[0][1][2])
    assert payload["action"] == "undraft_skip"
    assert payload["triggered_by"] == "ci_failed"


# ───────────────────────── auto_promote_pr — gh failures ─────────────────


@pytest.mark.asyncio
async def test_auto_promote_pr_view_failure_raises() -> None:
    from tpcore.lab.llm_emitter.auto_promote import (
        AutoPromoteError,
        auto_promote_pr,
    )

    pr_runner = _make_pr_runner(
        (1, "", "permission denied"),
    )
    pool = _FakePool()
    with pytest.raises(AutoPromoteError, match="gh pr view failed"):
        await auto_promote_pr(
            pool,  # type: ignore[arg-type]
            pr_url="https://github.com/foo/bar/pull/45",
            run_id="run-4",
            pr_runner=pr_runner,
        )


@pytest.mark.asyncio
async def test_auto_promote_pr_ready_failure_raises() -> None:
    from tpcore.lab.llm_emitter.auto_promote import (
        AutoPromoteError,
        auto_promote_pr,
    )

    pr_runner = _make_pr_runner(
        (0, _pr_view_json(), ""),
        (1, "", "PR cannot be made ready"),
    )
    pool = _FakePool()
    with pytest.raises(AutoPromoteError, match="gh pr ready failed"):
        await auto_promote_pr(
            pool,  # type: ignore[arg-type]
            pr_url="https://github.com/foo/bar/pull/46",
            run_id="run-5",
            pr_runner=pr_runner,
        )


@pytest.mark.asyncio
async def test_auto_promote_pr_merge_failure_raises() -> None:
    from tpcore.lab.llm_emitter.auto_promote import (
        AutoPromoteError,
        auto_promote_pr,
    )

    pr_runner = _make_pr_runner(
        (0, _pr_view_json(), ""),
        (0, "", ""),
        (1, "", "Merge conflict"),
    )
    pool = _FakePool()
    with pytest.raises(AutoPromoteError, match="gh pr merge failed"):
        await auto_promote_pr(
            pool,  # type: ignore[arg-type]
            pr_url="https://github.com/foo/bar/pull/47",
            run_id="run-6",
            pr_runner=pr_runner,
        )
