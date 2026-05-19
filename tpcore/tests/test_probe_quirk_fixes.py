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

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import ops  # noqa: E402

# ───── _check_trade_monitor_heartbeat ─────


def _hb_pool(row):
    """Build a fake asyncpg pool that returns ``row`` from fetchrow."""
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=row)
    return pool


@pytest.mark.asyncio
async def test_heartbeat_green_when_healthy_and_fresh() -> None:
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
async def test_heartbeat_red_when_degraded_even_if_fresh() -> None:
    row = {
        "last_heartbeat": datetime.now(UTC) - timedelta(minutes=5),
        "status": "degraded",
    }
    result = await ops._check_trade_monitor_heartbeat(_hb_pool(row))  # noqa: SLF001
    assert result["ok"] is False
    assert result["status"] == "degraded"
    assert "degraded" in result["reason"]


@pytest.mark.asyncio
async def test_heartbeat_red_when_stale_even_if_healthy() -> None:
    row = {
        "last_heartbeat": datetime.now(UTC) - timedelta(minutes=90),
        "status": "healthy",
    }
    result = await ops._check_trade_monitor_heartbeat(_hb_pool(row))  # noqa: SLF001
    assert result["ok"] is False
    assert result["status"] == "healthy"
    assert "stale" in result["reason"]


@pytest.mark.asyncio
async def test_heartbeat_red_when_status_down() -> None:
    row = {
        "last_heartbeat": datetime.now(UTC) - timedelta(minutes=5),
        "status": "down",
    }
    result = await ops._check_trade_monitor_heartbeat(_hb_pool(row))  # noqa: SLF001
    assert result["ok"] is False
    assert result["status"] == "down"


@pytest.mark.asyncio
async def test_heartbeat_red_when_row_missing() -> None:
    result = await ops._check_trade_monitor_heartbeat(_hb_pool(None))  # noqa: SLF001
    assert result["ok"] is False
    assert result["latest_event"] is None
    assert "no daemon_heartbeats row" in result["reason"]


# ───── _check_recent_errors ─────


@pytest.mark.asyncio
async def test_recent_errors_filters_noise_and_self_healed() -> None:
    """The probe issues two queries: one for the critical set (filtered)
    and one for the transient count. Verify each gets the right SQL and
    the response shape is correct.
    """
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
async def test_recent_errors_green_when_only_noise() -> None:
    """No critical rows + some transient/noise → ok=True."""
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchval = AsyncMock(return_value=5)

    result = await ops._check_recent_errors(pool)  # noqa: SLF001
    assert result["ok"] is True
    assert result["critical_count"] == 0
    assert result["transient_count"] == 5
    assert result["critical"] == []


@pytest.mark.asyncio
async def test_daemon_progress_no_recent_run() -> None:
    """No STARTUP within 25h → state='no_recent_run', ok=True."""
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=None)
    result = await ops._check_daemon_progress(pool)  # noqa: SLF001
    assert result["ok"] is True
    assert result["state"] == "no_recent_run"
    assert result["stages"] == []


@pytest.mark.asyncio
async def test_daemon_progress_running_in_flight() -> None:
    """STARTUP + some completed stages + one running, no SHUTDOWN → state='running'."""
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
async def test_daemon_progress_completed_clean() -> None:
    """All stages complete + SHUTDOWN exit_code=0 → state='completed_clean'."""
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
async def test_daemon_progress_completed_with_failures() -> None:
    """One INGESTION_FAILED stage → state='completed_with_failures', ok=False."""
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
async def test_recent_errors_handles_null_transient_count() -> None:
    """fetchval returns None when COUNT(*) somehow yields NULL — the
    probe must not crash on that path."""
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchval = AsyncMock(return_value=None)

    result = await ops._check_recent_errors(pool)  # noqa: SLF001
    assert result["ok"] is True
    assert result["transient_count"] == 0
