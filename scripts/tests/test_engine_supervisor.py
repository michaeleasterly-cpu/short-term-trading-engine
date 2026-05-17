import contextlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

# ops/ vs scripts/ops.py top-level name collision guard (identical to
# scripts/tests/test_engine_dispatch.py — repo root first, evict any
# non-package `ops`/`ops.*` so the real ops/ package resolves).
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

from ops import engine_supervisor as es  # noqa: E402


class _RecConn:
    def __init__(self):
        self.inserts: list[tuple] = []

    async def fetchrow(self, *_a, **_k):
        return None

    async def fetch(self, *_a, **_k):
        return []

    async def fetchval(self, *_a, **_k):
        return None

    async def execute(self, sql, *args):
        self.inserts.append((sql, args))


class _RecPool:
    def __init__(self):
        self.conn = _RecConn()

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield self.conn


async def test_emit_held_writes_locked_payload():
    pool = _RecPool()
    await es._emit_held(pool, "reversion", "h-1", "crashed_startup", "stale")
    sql, args = pool.conn.inserts[-1]
    assert "INSERT INTO platform.application_log" in sql
    payload = json.loads(args[-1])
    assert payload == {"schema": 1, "hold_id": "h-1", "engine": "reversion",
                       "failure_class": "crashed_startup", "reason": "stale"}
    assert args[2] == "ENGINE_HELD"


async def test_supervise_is_crash_isolated():
    # A detector raising must NOT propagate (sweep must never abort).
    with patch.object(es, "_detect_and_act",
                      new=AsyncMock(side_effect=RuntimeError("boom"))):
        await es.supervise(_RecPool(), "reversion",
                           datetime(2026, 5, 5, 21, 30, tzinfo=UTC),
                           AsyncMock())  # must not raise


def _rows_conn(rows_by_call):
    """A conn whose fetchrow returns queued rows in order, execute records."""
    class _C:
        def __init__(self):
            self.inserts = []
            self._q = list(rows_by_call)

        async def fetchrow(self, *_a, **_k):
            return self._q.pop(0) if self._q else None

        async def fetch(self, *_a, **_k):
            return []

        async def fetchval(self, *_a, **_k):
            return None

        async def execute(self, sql, *args):
            self.inserts.append((sql, args))
    return _C()


def _pool_for(conn):
    class _P:
        @contextlib.asynccontextmanager
        async def acquire(self):
            yield conn
    return _P()


async def test_crashed_startup_self_heals_then_recovered():
    now = datetime(2026, 5, 5, 21, 30, tzinfo=UTC)
    stale = datetime(2026, 5, 5, 14, 0, tzinfo=UTC)  # > 2h before now
    conn = _rows_conn([
        None,
        {"started_at": stale, "completed": False},
        {"started_at": now, "completed": True},
    ])
    invoke = AsyncMock()
    await es.supervise(_pool_for(conn), "reversion", now, invoke)
    invoke.assert_awaited()  # re-invoked as self-heal
    events = [a[2] for _s, a in conn.inserts]
    assert "ENGINE_SUPERVISOR_RECOVERED" in events
    assert "ENGINE_HELD" not in events


async def test_crashed_startup_unrecovered_escalates_and_holds():
    now = datetime(2026, 5, 5, 21, 30, tzinfo=UTC)
    stale = datetime(2026, 5, 5, 14, 0, tzinfo=UTC)
    rows = [None, {"started_at": stale, "completed": False}]
    rows += [{"started_at": stale, "completed": False}] * (es._MAX_REINVOKE + 1)
    conn = _rows_conn(rows)
    await es.supervise(_pool_for(conn), "reversion", now, AsyncMock())
    events = [a[2] for _s, a in conn.inserts]
    assert "ENGINE_ESCALATED" in events
    assert "ENGINE_HELD" in events
    assert "ENGINE_SUPERVISOR_RECOVERED" not in events


async def test_no_failure_no_events():
    now = datetime(2026, 5, 5, 21, 30, tzinfo=UTC)
    conn = _rows_conn([None, {"started_at": None, "completed": False}])
    invoke = AsyncMock()
    await es.supervise(_pool_for(conn), "reversion", now, invoke)
    invoke.assert_not_awaited()
    assert conn.inserts == []


async def test_already_held_skips_redetection_idempotent():
    now = datetime(2026, 5, 5, 21, 30, tzinfo=UTC)
    from tpcore.supervisor_state import HoldState
    held = HoldState("h-1", "crashed_startup", "stale",
                     datetime(2026, 5, 5, 14, 0, tzinfo=UTC))
    with patch.object(es, "current_hold", new=AsyncMock(return_value=held)), \
         patch.object(es, "_auto_clear", new=AsyncMock()) as clear:
        conn = _rows_conn([])
        await es.supervise(_pool_for(conn), "reversion", now, AsyncMock())
    assert all(a[2] != "ENGINE_HELD" for _s, a in conn.inserts)
    clear.assert_awaited_once()
