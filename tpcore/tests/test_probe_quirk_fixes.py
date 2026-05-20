"""Unit tests for the 2026-05-15 probe quirk fixes (P1 + P2).

Covers:
    * ``_check_trade_monitor_heartbeat`` — reads from new
      ``platform.daemon_heartbeats`` table; interprets status +
      timestamp jointly; replaces the application_log query.
    * ``_check_recent_errors`` — structured ``noise`` flag in
      data->>'noise' filters operator-expected failures; self-heal
      correlation filters errors whose run_id later succeeded.
"""
from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

# pytest-xdist: pin this ops-shadow module to one worker so its
# sys.modules['ops'] / scripts/ops.py loading stays single-process.
pytestmark = pytest.mark.xdist_group("ops_shadow")


@pytest.fixture
def ops_module():
    """Hermetic re-import of ``scripts/ops.py`` as the top-level ``ops``.

    Operator memory ``feedback_ops_package_shadow_full_suite_gate`` and
    ``.claude/rules/tests-and-ci.md`` forbid a collection-time
    ``sys.modules`` purge. This fixture moves the purge IN-BODY (test
    execution, not module collection) so each test gets a fresh,
    file-shadow ``ops`` even when an earlier test cached the ``ops/``
    package.

    Also forces ``scripts/`` ahead of the repo root on ``sys.path``
    because ``scripts/search_parameters.py`` deliberately reorders
    sys.path to put the repo root FIRST so its ``from ops.lab.run
    import …`` resolves the ``ops/`` package — a no-op ``insert(0, …)``
    check is not enough.
    """
    scripts_str = str(SCRIPTS_DIR)
    saved_path = list(sys.path)
    saved_ops = sys.modules.pop("ops", None)
    sys.path[:] = [p for p in sys.path if p != scripts_str]
    sys.path.insert(0, scripts_str)
    try:
        yield importlib.import_module("ops")
    finally:
        sys.modules.pop("ops", None)
        if saved_ops is not None:
            sys.modules["ops"] = saved_ops
        sys.path[:] = saved_path


# ───── _check_trade_monitor_heartbeat ─────


def _hb_pool(row):
    """Build a fake asyncpg pool that returns ``row`` from fetchrow."""
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=row)
    return pool


@pytest.mark.asyncio
async def test_heartbeat_green_when_healthy_and_fresh(ops_module) -> None:
    ops = ops_module
    row = {
        "last_heartbeat": datetime.now(UTC) - timedelta(minutes=5),
        "status": "healthy",
    }
    result = await ops._check_trade_monitor_heartbeat(_hb_pool(row))  # noqa: SLF001
    assert result["ok"] is True
    assert result["status"] == "healthy"
    assert result["age_minutes"] < 10
    assert "reason" not in result


@pytest.mark.asyncio
async def test_heartbeat_red_when_degraded_even_if_fresh(ops_module) -> None:
    ops = ops_module
    row = {
        "last_heartbeat": datetime.now(UTC) - timedelta(minutes=5),
        "status": "degraded",
    }
    result = await ops._check_trade_monitor_heartbeat(_hb_pool(row))  # noqa: SLF001
    assert result["ok"] is False
    assert result["status"] == "degraded"
    assert "degraded" in result["reason"]


@pytest.mark.asyncio
async def test_heartbeat_red_when_stale_even_if_healthy(ops_module) -> None:
    ops = ops_module
    row = {
        "last_heartbeat": datetime.now(UTC) - timedelta(minutes=90),
        "status": "healthy",
    }
    result = await ops._check_trade_monitor_heartbeat(_hb_pool(row))  # noqa: SLF001
    assert result["ok"] is False
    assert result["status"] == "healthy"
    assert "stale" in result["reason"]


@pytest.mark.asyncio
async def test_heartbeat_red_when_status_down(ops_module) -> None:
    ops = ops_module
    row = {
        "last_heartbeat": datetime.now(UTC) - timedelta(minutes=5),
        "status": "down",
    }
    result = await ops._check_trade_monitor_heartbeat(_hb_pool(row))  # noqa: SLF001
    assert result["ok"] is False
    assert result["status"] == "down"


@pytest.mark.asyncio
async def test_heartbeat_red_when_row_missing(ops_module) -> None:
    ops = ops_module
    result = await ops._check_trade_monitor_heartbeat(_hb_pool(None))  # noqa: SLF001
    assert result["ok"] is False
    assert result["latest_event"] is None
    assert "no daemon_heartbeats row" in result["reason"]


# ───── _check_recent_errors ─────


@pytest.mark.asyncio
async def test_recent_errors_filters_noise_and_self_healed(ops_module) -> None:
    """The probe issues two queries: one for the critical set (filtered)
    and one for the transient count. Verify each gets the right SQL and
    the response shape is correct.
    """
    ops = ops_module
    critical_rows = [
        {
            "engine": "ops",
            "event_type": "INGESTION_FAILED",
            "severity": "ERROR",
            "message": "real failure: cosmic-ray bitflip",
            "recorded_at": datetime.now(UTC) - timedelta(minutes=10),
            "run_id": "00000000-0000-0000-0000-000000000001",
        },
    ]

    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=critical_rows)
    pool.fetchval = AsyncMock(return_value=3)  # 3 transient rows excluded

    result = await ops._check_recent_errors(pool)  # noqa: SLF001

    assert result["ok"] is False  # one critical → not ok
    assert result["critical_count"] == 1
    assert result["transient_count"] == 3
    assert result["critical"][0]["message"] == "real failure: cosmic-ray bitflip"

    # Verify the critical query SQL filters by noise flag + self-heal.
    critical_call_sql = pool.fetch.call_args.args[0]
    assert "noise" in critical_call_sql
    assert "exit_code=0" in critical_call_sql
    assert "NOT EXISTS" in critical_call_sql

    # Verify the transient-count query is the inverse.
    transient_call_sql = pool.fetchval.call_args.args[0]
    assert "noise" in transient_call_sql
    assert "exit_code=0" in transient_call_sql


@pytest.mark.asyncio
async def test_recent_errors_green_when_only_noise(ops_module) -> None:
    """No critical rows + some transient/noise → ok=True."""
    ops = ops_module
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchval = AsyncMock(return_value=5)

    result = await ops._check_recent_errors(pool)  # noqa: SLF001
    assert result["ok"] is True
    assert result["critical_count"] == 0
    assert result["transient_count"] == 5
    assert result["critical"] == []


@pytest.mark.asyncio
async def test_daemon_progress_no_recent_run(ops_module) -> None:
    """No STARTUP within 25h → state='no_recent_run', ok=True."""
    ops = ops_module
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=None)
    result = await ops._check_daemon_progress(pool)  # noqa: SLF001
    assert result["ok"] is True
    assert result["state"] == "no_recent_run"
    assert result["stages"] == []


@pytest.mark.asyncio
async def test_daemon_progress_running_in_flight(ops_module) -> None:
    """STARTUP + some completed stages + one running, no SHUTDOWN → state='running'."""
    ops = ops_module
    import uuid as _uuid
    run_id = _uuid.UUID("11111111-1111-1111-1111-111111111111")
    started = datetime.now(UTC) - timedelta(minutes=20)
    pool = AsyncMock()
    # fetchrow used for both the startup query and the shutdown query.
    # Return startup, then None (no shutdown).
    pool.fetchrow = AsyncMock(side_effect=[
        {"run_id": run_id, "recorded_at": started},
        None,
    ])
    # Per-stage events: two complete + one mid-flight.
    pool.fetch = AsyncMock(return_value=[
        {"stage": "daily_bars", "event_type": "INGESTION_START",
         "recorded_at": started + timedelta(seconds=10)},
        {"stage": "daily_bars", "event_type": "INGESTION_COMPLETE",
         "recorded_at": started + timedelta(minutes=5)},
        {"stage": "corporate_actions", "event_type": "INGESTION_START",
         "recorded_at": started + timedelta(minutes=5, seconds=10)},
        {"stage": "corporate_actions", "event_type": "INGESTION_COMPLETE",
         "recorded_at": started + timedelta(minutes=10)},
        {"stage": "fundamentals_refresh", "event_type": "INGESTION_START",
         "recorded_at": started + timedelta(minutes=10, seconds=10)},
    ])
    pool.fetchval = AsyncMock(return_value=None)

    result = await ops._check_daemon_progress(pool)  # noqa: SLF001
    assert result["ok"] is True
    assert result["state"] == "running"
    assert result["n_stages_completed"] == 2
    assert result["n_stages_failed"] == 0
    assert result["n_stages_running"] == 1
    # fundamentals_refresh should still be running.
    fundies = next(s for s in result["stages"] if s["stage"] == "fundamentals_refresh")
    assert fundies["status"] == "running"
    assert fundies["ended_at"] is None


@pytest.mark.asyncio
async def test_daemon_progress_completed_clean(ops_module) -> None:
    """All stages complete + SHUTDOWN exit_code=0 → state='completed_clean'."""
    ops = ops_module
    import uuid as _uuid
    run_id = _uuid.UUID("22222222-2222-2222-2222-222222222222")
    started = datetime.now(UTC) - timedelta(hours=1)
    ended = started + timedelta(minutes=45)
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(side_effect=[
        {"run_id": run_id, "recorded_at": started},
        {"recorded_at": ended, "message": "ops CLI finished (exit_code=0)"},
    ])
    pool.fetch = AsyncMock(return_value=[
        {"stage": "daily_bars", "event_type": "INGESTION_START",
         "recorded_at": started + timedelta(seconds=10)},
        {"stage": "daily_bars", "event_type": "INGESTION_COMPLETE",
         "recorded_at": started + timedelta(minutes=5)},
    ])
    pool.fetchval = AsyncMock(return_value=1)  # DATA_OPERATIONS_COMPLETE found

    result = await ops._check_daemon_progress(pool)  # noqa: SLF001
    assert result["ok"] is True
    assert result["state"] == "completed_clean"
    assert result["n_stages_completed"] == 1
    assert result["workflow_complete"] is True


@pytest.mark.asyncio
async def test_daemon_progress_completed_with_failures(ops_module) -> None:
    """One INGESTION_FAILED stage → state='completed_with_failures', ok=False."""
    ops = ops_module
    import uuid as _uuid
    run_id = _uuid.UUID("33333333-3333-3333-3333-333333333333")
    started = datetime.now(UTC) - timedelta(hours=2)
    ended = started + timedelta(minutes=10)
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(side_effect=[
        {"run_id": run_id, "recorded_at": started},
        {"recorded_at": ended, "message": "ops CLI finished (exit_code=1)"},
    ])
    pool.fetch = AsyncMock(return_value=[
        {"stage": "daily_bars", "event_type": "INGESTION_START",
         "recorded_at": started + timedelta(seconds=10)},
        {"stage": "daily_bars", "event_type": "INGESTION_FAILED",
         "recorded_at": started + timedelta(minutes=2)},
    ])
    pool.fetchval = AsyncMock(return_value=None)

    result = await ops._check_daemon_progress(pool)  # noqa: SLF001
    assert result["ok"] is False
    assert result["state"] == "completed_with_failures"
    assert result["n_stages_failed"] == 1
    assert result["n_stages_completed"] == 0


@pytest.mark.asyncio
async def test_recent_errors_handles_null_transient_count(ops_module) -> None:
    """fetchval returns None when COUNT(*) somehow yields NULL — the
    probe must not crash on that path."""
    ops = ops_module
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchval = AsyncMock(return_value=None)

    result = await ops._check_recent_errors(pool)  # noqa: SLF001
    assert result["ok"] is True
    assert result["transient_count"] == 0
