import contextlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# scripts/ops.py (data-ops CLI) and the ops/ daemons package share the
# top-level name `ops`; tpcore/tests/test_ops.py does
# `sys.path.insert(0, scripts/); import ops`, so under full-suite
# collection sys.modules['ops'] is already bound to the scripts/ops.py
# MODULE (no .__path__) before this file is imported and Python won't
# re-resolve a cached name. Put repo root FIRST, then evict any
# non-package `ops`/`ops.*` so the real ops/ regular package
# (ops/__init__.py) resolves. The module OBJECT `es` is then bound from
# ONE import, so accessing names via `es.` is identity-stable regardless
# of full-suite sys.modules churn. (Root collision = pre-existing tech-debt.)
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

from ops import engine_service as es  # noqa: E402


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
    assert "DATA_OPERATIONS_COMPLETE" in es.TRIGGER_EVENT_TYPES
    assert "DATA_REPAIR_COMPLETE" in es.TRIGGER_EVENT_TYPES


async def test_find_new_trigger_returns_recorded_at_for_either_event():
    ts = datetime.now(UTC)
    got = await es._find_new_trigger(_Pool({"recorded_at": ts}), ts - timedelta(hours=1))
    assert got == ts


async def test_no_new_trigger_returns_none():
    got = await es._find_new_trigger(_Pool(None), datetime.now(UTC))
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

    got = await es._find_new_trigger(_CapPool(), datetime.now(UTC))
    assert got is None
    sql = captured["sql"]
    assert "DATA_REPAIR_COMPLETE" in sql
    assert "(data->>'green')::bool IS TRUE" in sql
