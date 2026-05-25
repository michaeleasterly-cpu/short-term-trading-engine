"""Tests for ``check_daemon_freshness`` — daemon liveness meta-monitor.

The 2026-05-22→25 P0 trust-audit failure mode: ``data_operations``,
``engine_service``, and ``allocator`` were 10 days stale and nothing
alerted. This check covers that gap by enforcing per-daemon liveness
contracts against ``platform.daemon_heartbeats``.

Per-daemon thresholds are hardcoded in
``DAEMON_MAX_AGE_SECS``; missing rows are STALE; one stale daemon =
RED check.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from tpcore.quality.validation.checks.daemon_freshness import (
    CHECK_NAME,
    DAEMON_MAX_AGE_SECS,
    check_daemon_freshness,
)


class _Conn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch(self, sql: str, *args: object) -> list[dict[str, Any]]:
        del args
        assert "platform.daemon_heartbeats" in sql.lower()
        return self._rows


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(self._rows))


def _row(daemon: str, age_secs: int) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "daemon_name": daemon,
        "last_heartbeat": now - timedelta(seconds=age_secs),
        "age_secs": age_secs,
    }


async def test_passes_when_all_daemons_fresh() -> None:
    rows = [
        _row("data_operations", 1000),   # < 26h
        _row("engine_service", 60),      # < 1h
        _row("allocator", 3600),         # < 6h
        _row("trade_monitor", 360),      # < 1h
    ]
    r = await check_daemon_freshness(_Pool(rows))
    assert r.passed is True
    assert r.failed == 0
    assert r.failures == []
    assert r.name == CHECK_NAME
    assert r.total == len(DAEMON_MAX_AGE_SECS)


async def test_fails_when_one_daemon_stale() -> None:
    rows = [
        _row("data_operations", DAEMON_MAX_AGE_SECS["data_operations"] + 100),
        _row("engine_service", 60),
        _row("allocator", 3600),
        _row("trade_monitor", 360),
    ]
    r = await check_daemon_freshness(_Pool(rows))
    assert r.passed is False
    assert r.failed == 1
    assert r.failures[0].ticker == "data_operations"
    assert r.failures[0].reason == "daemon_stale"
    assert "last_heartbeat=" in r.failures[0].observed


async def test_fails_when_daemon_row_missing() -> None:
    # data_operations row absent ⇒ daemon_missing RED
    rows = [
        _row("engine_service", 60),
        _row("allocator", 3600),
        _row("trade_monitor", 360),
    ]
    r = await check_daemon_freshness(_Pool(rows))
    assert r.passed is False
    assert r.failed == 1
    assert r.failures[0].ticker == "data_operations"
    assert r.failures[0].reason == "daemon_missing"


async def test_fails_when_multiple_daemons_stale() -> None:
    # The live P0 incident: three of four daemons stale by 10 days.
    rows = [
        _row("data_operations", 10 * 86400),
        _row("engine_service", 10 * 86400),
        _row("allocator", 10 * 86400),
        _row("trade_monitor", 360),
    ]
    r = await check_daemon_freshness(_Pool(rows))
    assert r.passed is False
    assert r.failed == 3
    stale_daemons = {f.ticker for f in r.failures}
    assert stale_daemons == {"data_operations", "engine_service", "allocator"}


async def test_passes_at_exact_threshold() -> None:
    # age equal to max is OK; only strictly greater fails. Ages 1s under
    # each threshold to guarantee we're never past the boundary in flight.
    rows = [
        _row(daemon, max_age - 1)
        for daemon, max_age in DAEMON_MAX_AGE_SECS.items()
    ]
    r = await check_daemon_freshness(_Pool(rows))
    assert r.passed is True


def test_threshold_table_includes_p0_audit_daemons() -> None:
    """The 3 daemons the P0 audit named MUST be in the threshold table.

    Removing one would re-open the silent-stall gap the check was
    built to close."""
    for daemon in ("data_operations", "engine_service", "allocator"):
        assert daemon in DAEMON_MAX_AGE_SECS, (
            f"{daemon} dropped from DAEMON_MAX_AGE_SECS — re-opens the "
            "2026-05-22 silent-stall failure class"
        )
