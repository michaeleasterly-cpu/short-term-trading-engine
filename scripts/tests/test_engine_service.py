import contextlib
from datetime import UTC, datetime, timedelta

from ops.engine_service import TRIGGER_EVENT_TYPES, _find_new_trigger


class _Conn:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, sql, *args):
        # the SQL must filter on a SET of event types, not a single $1
        assert "ANY(" in sql, sql
        # only a *green* DATA_REPAIR_COMPLETE may unblock an engine
        assert "data->>'green'" in sql, sql
        return self._row


class _Pool:
    def __init__(self, row):
        self._row = row

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield _Conn(self._row)


async def test_trigger_set_includes_both_events():
    assert "DATA_OPERATIONS_COMPLETE" in TRIGGER_EVENT_TYPES
    assert "DATA_REPAIR_COMPLETE" in TRIGGER_EVENT_TYPES


async def test_find_new_trigger_returns_recorded_at_for_either_event():
    ts = datetime.now(UTC)
    got = await _find_new_trigger(_Pool({"recorded_at": ts}), ts - timedelta(hours=1))
    assert got == ts


async def test_no_new_trigger_returns_none():
    got = await _find_new_trigger(_Pool(None), datetime.now(UTC))
    assert got is None


async def test_non_green_repair_complete_filtered():
    # A non-green DATA_REPAIR_COMPLETE must not unblock an engine; the
    # query carries an explicit green-only clause for DATA_REPAIR_COMPLETE
    # so a red repair is filtered server-side (returns no row -> None).
    captured = {}

    class _CapConn:
        async def fetchrow(self, sql, *args):
            captured["sql"] = sql
            return None

    class _CapPool:
        @contextlib.asynccontextmanager
        async def acquire(self):
            yield _CapConn()

    got = await _find_new_trigger(_CapPool(), datetime.now(UTC))
    assert got is None
    sql = captured["sql"]
    assert "DATA_REPAIR_COMPLETE" in sql
    assert "(data->>'green')::bool IS TRUE" in sql
