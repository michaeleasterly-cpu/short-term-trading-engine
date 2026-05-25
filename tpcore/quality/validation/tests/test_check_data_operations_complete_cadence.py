"""Tests for ``check_data_operations_complete_cadence`` — lane meta-monitor.

The P0 trust-audit finding: ``DATA_OPERATIONS_COMPLETE`` has NEVER
been emitted in live history despite 121 partial ``INGESTION_COMPLETE``
markers — the gate worked, but nothing surfaced the absence of the
gate event. This check covers that gap directly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from tpcore.quality.validation.checks.data_operations_complete_cadence import (
    CHECK_NAME,
    MAX_AGE_SECS,
    check_data_operations_complete_cadence,
)


class _Conn:
    def __init__(self, last_emit: Any, total: int) -> None:
        self._last_emit = last_emit
        self._total = total

    async def fetchrow(self, sql: str, *args: object) -> dict[str, Any]:
        del args
        sl = sql.lower()
        assert "platform.application_log" in sl
        assert "data_operations_complete" in sl
        return {"last_emit": self._last_emit, "total_emits": self._total}


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(self, last_emit: Any, total: int = 1) -> None:
        self._last_emit = last_emit
        self._total = total

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(self._last_emit, self._total))


async def test_fails_when_never_emitted() -> None:
    """The live state at audit time: zero rows ever. RED."""
    r = await check_data_operations_complete_cadence(_Pool(None, total=0))
    assert r.passed is False
    assert r.failed == 1
    assert r.failures[0].ticker == "<lane>"
    assert r.failures[0].reason == "never_emitted"
    assert "ZERO rows" in r.failures[0].observed
    assert r.name == CHECK_NAME


async def test_fails_when_stale() -> None:
    stale_when = datetime.now(UTC) - timedelta(seconds=MAX_AGE_SECS + 3600)
    r = await check_data_operations_complete_cadence(_Pool(stale_when, total=5))
    assert r.passed is False
    assert r.failed == 1
    assert r.failures[0].reason == "lane_stale"
    assert "h ago" in r.failures[0].observed


async def test_passes_when_recent() -> None:
    fresh_when = datetime.now(UTC) - timedelta(minutes=30)
    r = await check_data_operations_complete_cadence(_Pool(fresh_when, total=42))
    assert r.passed is True
    assert r.failed == 0
    assert r.failures == []


async def test_passes_at_boundary() -> None:
    # 1s under the threshold ⇒ still PASS.
    boundary_when = datetime.now(UTC) - timedelta(seconds=MAX_AGE_SECS - 1)
    r = await check_data_operations_complete_cadence(_Pool(boundary_when, total=1))
    assert r.passed is True


def test_max_age_is_at_least_24h() -> None:
    """The operator-stated contract floor: lane runs at least once/day.
    The check window must allow for the contract + a small grace; any
    value < 24h would red on a single skipped run."""
    assert MAX_AGE_SECS >= 24 * 3600
