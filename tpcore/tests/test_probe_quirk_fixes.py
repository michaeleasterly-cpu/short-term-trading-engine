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
    result = await ops._check_trade_monitor_heartbeat(_hb_pool(row))
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
    result = await ops._check_trade_monitor_heartbeat(_hb_pool(row))
    assert result["ok"] is False
    assert result["status"] == "degraded"
    assert "degraded" in result["reason"]


@pytest.mark.asyncio
async def test_heartbeat_red_when_stale_even_if_healthy() -> None:
    row = {
        "last_heartbeat": datetime.now(UTC) - timedelta(minutes=90),
        "status": "healthy",
    }
    result = await ops._check_trade_monitor_heartbeat(_hb_pool(row))
    assert result["ok"] is False
    assert result["status"] == "healthy"
    assert "stale" in result["reason"]


@pytest.mark.asyncio
async def test_heartbeat_red_when_status_down() -> None:
    row = {
        "last_heartbeat": datetime.now(UTC) - timedelta(minutes=5),
        "status": "down",
    }
    result = await ops._check_trade_monitor_heartbeat(_hb_pool(row))
    assert result["ok"] is False
    assert result["status"] == "down"


@pytest.mark.asyncio
async def test_heartbeat_red_when_row_missing() -> None:
    result = await ops._check_trade_monitor_heartbeat(_hb_pool(None))
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

    result = await ops._check_recent_errors(pool)

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

    result = await ops._check_recent_errors(pool)
    assert result["ok"] is True
    assert result["critical_count"] == 0
    assert result["transient_count"] == 5
    assert result["critical"] == []


@pytest.mark.asyncio
async def test_recent_errors_handles_null_transient_count() -> None:
    """fetchval returns None when COUNT(*) somehow yields NULL — the
    probe must not crash on that path."""
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchval = AsyncMock(return_value=None)

    result = await ops._check_recent_errors(pool)
    assert result["ok"] is True
    assert result["transient_count"] == 0
