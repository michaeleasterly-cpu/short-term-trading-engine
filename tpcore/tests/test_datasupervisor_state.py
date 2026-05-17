"""Unit tests for the data-supervisor event-sourced hold read.

Pure: a fake asyncpg pool returning scripted rows. No DB. Mirrors the
fake-pool style of tpcore/tests/test_selfheal.py.
"""
from __future__ import annotations

from datetime import UTC, datetime

from tpcore.datasupervisor.state import (
    CLEARED_EVENT,
    ESCALATED_EVENT,
    HELD_EVENT,
    RECOVERED_EVENT,
    SCHEMA_VERSION,
    SourceHoldState,
    current_source_hold,
)


class _Conn:
    def __init__(self, row):
        self._row = row
        self.calls: list[tuple] = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self._row


class _CM:
    def __init__(self, c):
        self._c = c

    async def __aenter__(self):
        return self._c

    async def __aexit__(self, *e):
        return None


class _Pool:
    def __init__(self, row):
        self.conn = _Conn(row)

    def acquire(self):
        return _CM(self.conn)


def test_constants_locked() -> None:
    assert SCHEMA_VERSION == 1
    assert HELD_EVENT == "DATA_SOURCE_HELD"
    assert CLEARED_EVENT == "DATA_SOURCE_CLEARED"
    assert ESCALATED_EVENT == "DATA_SOURCE_ESCALATED"
    assert RECOVERED_EVENT == "DATA_SUPERVISOR_RECOVERED"  # deliberate: mirrors engine ENGINE_SUPERVISOR_RECOVERED (NOT DATA_SOURCE_RECOVERED)


async def test_open_hold_returned() -> None:
    now = datetime(2026, 5, 17, tzinfo=UTC)
    pool = _Pool({
        "hold_id": "h1", "reason": "validation:prices_daily red",
        "held_at": now, "cleared": None,
    })
    hold = await current_source_hold(pool, "validation:prices_daily")
    assert isinstance(hold, SourceHoldState)
    assert hold.hold_id == "h1" and hold.held_at == now
    assert hold.reason == "validation:prices_daily red"
    sql, args = pool.conn.calls[0]
    assert args == ("DATA_SOURCE_HELD", "DATA_SOURCE_CLEARED",
                    "validation:prices_daily")
    assert "h.data->>'source' = $3" in sql


async def test_cleared_hold_is_none() -> None:
    pool = _Pool({
        "hold_id": "h1", "reason": "x",
        "held_at": datetime(2026, 5, 17, tzinfo=UTC),
        "cleared": "DATA_SOURCE_CLEARED",
    })
    assert await current_source_hold(pool, "contract:fred_macro") is None


async def test_no_row_is_none() -> None:
    assert await current_source_hold(_Pool(None), "cross_table:x") is None
