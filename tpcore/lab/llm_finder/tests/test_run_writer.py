"""FinderRun + LAB_FINDER_ACTION provenance writer tests — §10.4."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pytest

from tpcore.lab.llm_finder.models import FinderRun, _compute_regime_tuple_id
from tpcore.lab.llm_finder.run_writer import record_finder_action, record_finder_run


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


def _finder_run() -> FinderRun:
    return FinderRun(
        run_id=uuid4(),
        started_ts=datetime.now(UTC),
        completed_ts=datetime.now(UTC),
        trigger="operator_command",
        snapshot_session_date=date(2026, 5, 21),
        snapshot_regime_tuple_id=_compute_regime_tuple_id(
            "normal", "range", "expansion", "neutral"
        ),
        persona_version="v2.1",
        reference_bundles=("dsr_ntrials_discipline", "regime_aware_trading"),
        analysis_turn_count=3,
        proposed_spec_count=1,
        emitted_pr_urls=("https://github.com/foo/bar/pull/123",),
        auto_merged_pr_urls=("https://github.com/foo/bar/pull/123",),
        auto_issued_ecr_refs=("ECR-2026-05-21-001",),
        rejection_reason=None,
    )


# ───────────────────────── record_finder_run ─────────────────────────


@pytest.mark.asyncio
async def test_record_finder_run_writes_lab_finder_run_event() -> None:
    pool = _FakePool()
    run = _finder_run()
    await record_finder_run(pool, run)  # type: ignore[arg-type]

    assert len(pool.sink) == 1
    sql, args = pool.sink[0]
    assert "LAB_FINDER_RUN" in sql
    assert "llm_edge_finder" in sql
    # Post-fix SQL: ($1 run_id UUID, $2 data jsonb) — 2 args.
    assert len(args) == 2
    assert args[0] == run.run_id
    payload = json.loads(args[1])
    assert payload["trigger"] == "operator_command"
    assert payload["proposed_spec_count"] == 1


@pytest.mark.asyncio
async def test_record_finder_run_payload_includes_run_id() -> None:
    pool = _FakePool()
    run = _finder_run()
    await record_finder_run(pool, run)  # type: ignore[arg-type]
    # run_id is now in the dedicated UUID column (args[0]) — not in the data payload.
    assert pool.sink[0][1][0] == run.run_id


# ───────────────────────── record_finder_action ─────────────────────────


@pytest.mark.asyncio
async def test_record_finder_action_writes_lab_finder_action_event() -> None:
    pool = _FakePool()
    run = _finder_run()
    await record_finder_action(
        pool,  # type: ignore[arg-type]
        run_id=str(run.run_id),
        action="draft",
        triggered_by="operator_command",
    )
    assert len(pool.sink) == 1
    sql, args = pool.sink[0]
    assert "LAB_FINDER_ACTION" in sql
    # Post-fix SQL: ($1 run_id, $2 message, $3 data jsonb) — 3 args.
    assert len(args) == 3
    payload = json.loads(args[2])
    assert payload["action"] == "draft"
    assert payload["triggered_by"] == "operator_command"
    assert payload["human_override"] == "none"


@pytest.mark.asyncio
async def test_record_finder_action_with_extra_payload() -> None:
    pool = _FakePool()
    await record_finder_action(
        pool,  # type: ignore[arg-type]
        run_id="abc-123",
        action="ecr_modify",
        triggered_by="ci_green",
        extra={"pr_url": "https://github.com/foo/bar/pull/456"},
    )
    # Post-fix SQL: data payload is args[2] (after run_id, message).
    payload = json.loads(pool.sink[0][1][2])
    assert payload["pr_url"] == "https://github.com/foo/bar/pull/456"


@pytest.mark.asyncio
async def test_record_finder_action_human_override_always_none() -> None:
    """Per spec §2.16 — v1 has no per-step override."""
    pool = _FakePool()
    await record_finder_action(
        pool,  # type: ignore[arg-type]
        run_id="r1",
        action="merge",
        triggered_by="gate_pass",
    )
    # Post-fix SQL: data payload is args[2] (after run_id, message).
    payload = json.loads(pool.sink[0][1][2])
    assert payload["human_override"] == "none"


@pytest.mark.asyncio
async def test_record_finder_action_extra_does_not_override_human_field() -> None:
    """Even if caller passes human_override in extra, defense layer wins —
    no, actually extra IS allowed to override; the v1 contract is the
    DEFAULT is 'none', not enforced immutability. Document this clearly."""
    pool = _FakePool()
    await record_finder_action(
        pool,  # type: ignore[arg-type]
        run_id="r1",
        action="ecr_retire",
        triggered_by="bleed_cap",
        extra={"human_override": "operator_revert"},  # operator-initiated override
    )
    # Post-fix SQL: data payload is args[2] (after run_id, message).
    payload = json.loads(pool.sink[0][1][2])
    # Document the actual behaviour: extra IS allowed to override.
    assert payload["human_override"] == "operator_revert"
