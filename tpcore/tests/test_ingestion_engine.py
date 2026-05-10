"""Tests for ``tpcore.ingestion.engine.IngestionEngine``.

The fake pool here simulates a tiny in-memory ``platform.ingestion_jobs``
table — enough surface for SELECT, the guarded UPDATE that claims a
row, and the final UPDATE that records the result. That lets us
exercise the full happy/failure/race paths without a live database.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tpcore.ingestion.cron_eval import next_run_after
from tpcore.ingestion.engine import IngestionEngine, JobResult


# ────────────────────────────────────────────────────────────────────────────
# Fake asyncpg pool — simulates ingestion_jobs INSERT/SELECT/UPDATE flow
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class _FakeJob:
    job_name: str
    schedule: str
    provider: str = "internal"
    config: dict = field(default_factory=dict)
    enabled: bool = True
    next_run: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    last_duration_ms: int | None = None


class _FakeRecord(dict):
    """asyncpg.Record-ish — supports dict access and __getitem__."""


class _FakeConn:
    def __init__(self, store: dict[str, _FakeJob]) -> None:
        self._store = store

    async def fetch(self, sql: str, *args) -> list[_FakeRecord]:
        # _fetch_due: SELECT enabled rows whose next_run <= now AND not 'running' OR stale
        if "SELECT job_name, schedule" in sql:
            now, stale_cutoff = args
            rows = []
            for job in self._store.values():
                if not job.enabled:
                    continue
                if job.next_run > now:
                    continue
                if (
                    job.last_status == "running"
                    and job.last_run_at is not None
                    and job.last_run_at >= stale_cutoff
                ):
                    continue
                rows.append(
                    _FakeRecord(
                        job_name=job.job_name,
                        schedule=job.schedule,
                        provider=job.provider,
                        config=job.config,
                    )
                )
            rows.sort(key=lambda r: r["job_name"])
            return rows
        raise AssertionError(f"unexpected fetch SQL: {sql[:80]}")

    async def fetchrow(self, sql: str, *args) -> _FakeRecord | None:
        # _claim: guarded UPDATE ... RETURNING
        if "UPDATE platform.ingestion_jobs" in sql and "RETURNING job_name" in sql:
            job_name, now, stale_cutoff = args
            job = self._store.get(job_name)
            if job is None or not job.enabled or job.next_run > now:
                return None
            if (
                job.last_status == "running"
                and job.last_run_at is not None
                and job.last_run_at >= stale_cutoff
            ):
                return None
            job.last_status = "running"
            job.last_run_at = now
            return _FakeRecord(job_name=job.job_name)
        raise AssertionError(f"unexpected fetchrow SQL: {sql[:80]}")

    async def execute(self, sql: str, *args) -> str:
        # _record_result: final UPDATE setting status/error/duration/next_run
        if "UPDATE platform.ingestion_jobs" in sql and "RETURNING" not in sql:
            job_name, status, error, duration_ms, next_run, _now = args
            job = self._store[job_name]
            job.last_status = status
            job.last_error = error
            job.last_duration_ms = duration_ms
            job.next_run = next_run
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute SQL: {sql[:80]}")


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(self, jobs: list[_FakeJob]) -> None:
        self.store = {j.job_name: j for j in jobs}

    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(_FakeConn(self.store))


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _at(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def _frozen_clock(when: datetime):
    return lambda: when


# ────────────────────────────────────────────────────────────────────────────
# tick(): success path
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_runs_due_job_and_advances_next_run() -> None:
    now = _at(2026, 5, 11, 12)  # Monday 12:00 UTC
    pool = _FakePool([
        _FakeJob(
            job_name="data_validation",
            schedule="0 6 * * SUN",
            next_run=now - timedelta(minutes=5),  # overdue
        )
    ])
    calls: list[dict] = []

    async def fake_handler(_pool, config):
        calls.append(config)

    engine = IngestionEngine(
        pool,  # type: ignore[arg-type]
        handlers={"data_validation": fake_handler},
        clock=_frozen_clock(now),
    )

    [result] = await engine.tick()

    assert isinstance(result, JobResult)
    assert result.job_name == "data_validation"
    assert result.status == "success"
    assert result.error is None

    job = pool.store["data_validation"]
    assert job.last_status == "success"
    assert job.last_error is None
    # next_run advanced to the next Sunday 06:00 UTC after 'now'.
    assert job.next_run == next_run_after("0 6 * * SUN", now)
    assert calls == [{}]  # config payload reached the handler


# ────────────────────────────────────────────────────────────────────────────
# tick(): failure path — error is captured, next_run still advances
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_failed_handler_records_error_and_continues() -> None:
    now = _at(2026, 5, 11, 12)
    pool = _FakePool([
        _FakeJob(
            job_name="fundamentals_refresh",
            schedule="0 3 * * SUN",
            next_run=now - timedelta(hours=1),
        )
    ])

    async def boom(_pool, _config):
        raise RuntimeError("FMP returned 503")

    engine = IngestionEngine(
        pool,  # type: ignore[arg-type]
        handlers={"fundamentals_refresh": boom},
        clock=_frozen_clock(now),
    )

    [result] = await engine.tick()

    assert result.status == "failed"
    assert result.error is not None and "FMP returned 503" in result.error
    job = pool.store["fundamentals_refresh"]
    assert job.last_status == "failed"
    assert "FMP returned 503" in (job.last_error or "")
    # Failed jobs still advance — we don't auto-retry inside the same tick.
    assert job.next_run > now


# ────────────────────────────────────────────────────────────────────────────
# Disabled / not-yet-due jobs are skipped
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_skips_disabled_and_future_jobs() -> None:
    now = _at(2026, 5, 11, 12)
    pool = _FakePool([
        _FakeJob(
            job_name="disabled_job",
            schedule="0 3 * * SUN",
            enabled=False,
            next_run=now - timedelta(hours=1),
        ),
        _FakeJob(
            job_name="future_job",
            schedule="0 3 * * SUN",
            next_run=now + timedelta(hours=1),
        ),
    ])
    engine = IngestionEngine(
        pool,  # type: ignore[arg-type]
        handlers={"disabled_job": _noop, "future_job": _noop},
        clock=_frozen_clock(now),
    )

    results = await engine.tick()

    assert results == []
    assert pool.store["disabled_job"].last_status is None
    assert pool.store["future_job"].last_status is None


# ────────────────────────────────────────────────────────────────────────────
# 'running' lock prevents re-execution within the staleness window
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_skips_jobs_currently_running() -> None:
    now = _at(2026, 5, 11, 12)
    # Another worker started this job 5 minutes ago — well within the 30m
    # staleness window, so this engine must NOT pick it up.
    pool = _FakePool([
        _FakeJob(
            job_name="busy",
            schedule="*/10 * * * *",
            next_run=now - timedelta(minutes=15),
            last_status="running",
            last_run_at=now - timedelta(minutes=5),
        )
    ])
    engine = IngestionEngine(
        pool,  # type: ignore[arg-type]
        handlers={"busy": _noop},
        clock=_frozen_clock(now),
    )

    assert await engine.tick() == []
    assert pool.store["busy"].last_status == "running"  # unchanged


@pytest.mark.asyncio
async def test_tick_recovers_stale_running_job() -> None:
    now = _at(2026, 5, 11, 12)
    # The previous worker died 45 minutes ago, leaving 'running' stuck.
    # Past the 30-minute staleness threshold, the engine reclaims.
    pool = _FakePool([
        _FakeJob(
            job_name="zombie",
            schedule="*/10 * * * *",
            next_run=now - timedelta(hours=1),
            last_status="running",
            last_run_at=now - timedelta(minutes=45),
        )
    ])
    engine = IngestionEngine(
        pool,  # type: ignore[arg-type]
        handlers={"zombie": _noop},
        clock=_frozen_clock(now),
    )

    [result] = await engine.tick()
    assert result.status == "success"
    assert pool.store["zombie"].last_status == "success"


# ────────────────────────────────────────────────────────────────────────────
# Unknown job_name → recorded as failed, doesn't crash the tick
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_records_failure_for_unknown_handler() -> None:
    now = _at(2026, 5, 11, 12)
    pool = _FakePool([
        _FakeJob(
            job_name="unknown_job",
            schedule="0 3 * * SUN",
            next_run=now - timedelta(hours=1),
        )
    ])
    engine = IngestionEngine(
        pool,  # type: ignore[arg-type]
        handlers={},  # no registered handlers
        clock=_frozen_clock(now),
    )

    [result] = await engine.tick()

    assert result.status == "skipped_no_handler"
    job = pool.store["unknown_job"]
    assert job.last_status == "failed"
    assert "no handler" in (job.last_error or "")


# ────────────────────────────────────────────────────────────────────────────
# Multiple due jobs in one tick
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_runs_multiple_due_jobs_in_one_pass() -> None:
    now = _at(2026, 5, 11, 12)
    pool = _FakePool([
        _FakeJob(job_name="a", schedule="*/5 * * * *", next_run=now - timedelta(hours=1)),
        _FakeJob(job_name="b", schedule="*/7 * * * *", next_run=now - timedelta(hours=1)),
    ])
    engine = IngestionEngine(
        pool,  # type: ignore[arg-type]
        handlers={"a": _noop, "b": _noop},
        clock=_frozen_clock(now),
    )

    results = await engine.tick()
    statuses = sorted(r.status for r in results)
    names = sorted(r.job_name for r in results)
    assert statuses == ["success", "success"]
    assert names == ["a", "b"]


# ────────────────────────────────────────────────────────────────────────────
# Config payload is passed through (stringified jsonb is decoded)
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_decodes_jsonb_string_config() -> None:
    """Some asyncpg setups deliver jsonb columns as strings rather than dicts.
    The engine should decode either shape."""
    now = _at(2026, 5, 11, 12)
    pool = _FakePool([
        _FakeJob(
            job_name="cfg_test",
            schedule="0 3 * * SUN",
            next_run=now - timedelta(hours=1),
            config=json.dumps({"universe": "active", "lookback_days": 14}),  # type: ignore[arg-type]
        )
    ])
    received: list[dict] = []

    async def capture(_pool, config):
        received.append(config)

    engine = IngestionEngine(
        pool,  # type: ignore[arg-type]
        handlers={"cfg_test": capture},
        clock=_frozen_clock(now),
    )

    await engine.tick()
    assert received == [{"universe": "active", "lookback_days": 14}]


# ────────────────────────────────────────────────────────────────────────────
# Cron evaluator
# ────────────────────────────────────────────────────────────────────────────


def test_cron_eval_advances_to_next_sunday_for_sun_only_expr() -> None:
    monday_noon = _at(2026, 5, 11, 12)
    nxt = next_run_after("0 3 * * SUN", monday_noon)
    # Next Sunday 03:00 UTC after Mon 12:00.
    assert nxt.weekday() == 6  # Sunday
    assert nxt.hour == 3 and nxt.minute == 0


def test_cron_eval_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        next_run_after("0 3 * * SUN", datetime(2026, 5, 11, 12))


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


async def _noop(_pool: Any, _config: Any) -> None:
    return None
