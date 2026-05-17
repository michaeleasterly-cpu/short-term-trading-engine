import contextlib
from datetime import UTC, datetime

from tpcore.supervisor_state import (
    CLEARED_EVENT,
    ESCALATED_EVENT,
    HELD_EVENT,
    RECOVERED_EVENT,
    SCHEMA_VERSION,
    HoldState,
    current_hold,
)


class _Conn:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, *_a, **_k):
        return self._row


class _Pool:
    def __init__(self, row):
        self._row = row

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield _Conn(self._row)


def test_event_vocabulary_is_locked():
    assert HELD_EVENT == "ENGINE_HELD"
    assert CLEARED_EVENT == "ENGINE_CLEARED"
    assert ESCALATED_EVENT == "ENGINE_ESCALATED"
    assert RECOVERED_EVENT == "ENGINE_SUPERVISOR_RECOVERED"
    assert SCHEMA_VERSION == 1


async def test_current_hold_none_when_no_held_row():
    assert await current_hold(_Pool(None), "reversion") is None


async def test_current_hold_returns_holdstate_when_held_unclearedd():
    row = {
        "hold_id": "h-1",
        "failure_class": "crashed_startup",
        "reason": "stale STARTUP",
        "held_at": datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
        "cleared": None,
    }
    hs = await current_hold(_Pool(row), "reversion")
    assert isinstance(hs, HoldState)
    assert hs.hold_id == "h-1"
    assert hs.failure_class == "crashed_startup"
    assert hs.held_at == datetime(2026, 5, 5, 21, 0, tzinfo=UTC)


async def test_current_hold_none_when_latest_held_is_cleared():
    row = {
        "hold_id": "h-1",
        "failure_class": "crashed_startup",
        "reason": "stale STARTUP",
        "held_at": datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
        "cleared": "ENGINE_CLEARED",
    }
    assert await current_hold(_Pool(row), "reversion") is None
