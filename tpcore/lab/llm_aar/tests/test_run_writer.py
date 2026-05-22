"""Provenance writer tests — spec §3.2.

Verifies that AARCriticRun + AARFinding rows land in platform.application_log
via the LAB_AAR_CRITIC_RUN + LAB_AAR_CRITIC_FINDING event types.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pytest

from tpcore.lab.llm_aar.models import AARCriticRun, AARFinding, compute_finding_id
from tpcore.lab.llm_aar.run_writer import (
    count_runs_in_utc_day,
    record_aar_critic_run,
    record_aar_finding,
)


class _FakeConn:
    def __init__(self, capture: list[tuple[str, tuple[Any, ...]]] | None = None) -> None:
        self._capture = capture if capture is not None else []
        self.count_value: int = 0  # public; tests drive the fetchval helper

    async def execute(self, sql: str, *args: Any) -> None:
        self._capture.append((sql, args))

    async def fetchval(self, _sql: str, *_args: Any) -> int:
        return self.count_value


class _AcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakePool:
    def __init__(self) -> None:
        self._captured: list[tuple[str, tuple[Any, ...]]] = []
        self.conn = _FakeConn(self._captured)  # public; tests configure the conn

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(self.conn)

    @property
    def captured(self) -> list[tuple[str, tuple[Any, ...]]]:
        return self._captured


@pytest.mark.asyncio
async def test_record_aar_critic_run_writes_provenance() -> None:
    pool = _FakePool()
    run = AARCriticRun(
        run_id=uuid4(),
        started_ts=datetime.now(UTC),
        completed_ts=datetime.now(UTC),
        trigger="operator_command",
        as_of_session=date(2026, 5, 22),
        engines_examined=("catalyst", "vector"),
        findings_emitted=("aaaaaaaaaaaa", "bbbbbbbbbbbb"),
        persona_version="v1.0",
        rejection_reason=None,
    )
    await record_aar_critic_run(pool, run)  # type: ignore[arg-type]
    assert len(pool.captured) == 1
    sql, args = pool.captured[0]
    assert "LAB_AAR_CRITIC_RUN" in sql
    assert args[0] == run.run_id
    # Payload jsonb carries the model dump
    payload = args[1]
    assert "operator_command" in payload
    assert "v1.0" in payload


@pytest.mark.asyncio
async def test_record_aar_finding_writes_per_finding_row() -> None:
    pool = _FakePool()
    fid = compute_finding_id("catalyst", "exit_timing", date(2026, 5, 22))
    finding = AARFinding(
        engine="catalyst",
        finding_id=fid,
        theme="exit_timing",
        pattern_observed="Time-stop exits skew negative.",
        suggested_emission_axis="Test 10-session hold variant.",
        evidence_aar_count=9,
        evidence_window_sessions=90,
        confidence="medium",
        observation_session=date(2026, 5, 22),
        persona_version="v1.0",
    )
    run_id = str(uuid4())
    await record_aar_finding(pool, run_id=run_id, finding=finding)  # type: ignore[arg-type]
    assert len(pool.captured) == 1
    sql, args = pool.captured[0]
    assert "LAB_AAR_CRITIC_FINDING" in sql
    # finding_id + engine in the message
    assert "catalyst" in args[1]
    assert fid in args[1]
    # payload jsonb carries the finding
    assert "exit_timing" in args[2]


@pytest.mark.asyncio
async def test_record_aar_finding_handles_bad_run_id() -> None:
    """Non-UUID run_id falls back to NIL UUID; does not raise."""
    pool = _FakePool()
    fid = compute_finding_id("catalyst", "exit_timing", date(2026, 5, 22))
    finding = AARFinding(
        engine="catalyst",
        finding_id=fid,
        theme="exit_timing",
        pattern_observed="x",
        suggested_emission_axis="y",
        evidence_aar_count=3,
        evidence_window_sessions=10,
        confidence="low",
        observation_session=date(2026, 5, 22),
        persona_version="v1.0",
    )
    await record_aar_finding(pool, run_id="not-a-uuid", finding=finding)  # type: ignore[arg-type]
    # Still captured one row
    assert len(pool.captured) == 1


@pytest.mark.asyncio
async def test_count_runs_in_utc_day_returns_int() -> None:
    """Rate-ceiling check helper returns a non-negative integer."""
    pool = _FakePool()
    pool.conn.count_value = 1
    n = await count_runs_in_utc_day(
        pool,  # type: ignore[arg-type]
        "2026-05-22T00:00:00",
        "2026-05-23T00:00:00",
    )
    assert n == 1
