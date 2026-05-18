import asyncio
import contextlib
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: F401

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


async def test_shared_pool_built_once_and_closed_once(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://fake/db")
    built = []

    class _P:
        def __init__(self):
            self.closed = 0

        async def close(self):
            self.closed += 1

    async def _fake_build(dsn, **kw):
        p = _P()
        built.append((p, kw))
        return p
    monkeypatch.setattr(es, "build_asyncpg_pool", _fake_build)
    # monitor construction must not require live ALPACA creds — the
    # invariant under test is pool-built-once / closed-once, not the
    # broker. Stub the broker + monitor constructors.
    monkeypatch.setattr(es, "AlpacaPaperBrokerAdapter", MagicMock())
    monkeypatch.setattr(es, "TradeMonitor", MagicMock())
    monkeypatch.setattr(es, "AARWriter", MagicMock())
    # both co-tasks return immediately so _amain falls through
    monkeypatch.setattr(es, "_run_supervised",
                        AsyncMock(return_value=None))
    rc = await es._amain()
    assert rc == 0
    assert len(built) == 1                       # pool built exactly once
    assert built[0][1].get("max_size", 0) >= 5   # H-8 sizing
    assert built[0][0].closed == 1               # closed exactly once


async def test_supervised_restarts_crashed_task_without_killing_sibling():
    calls = {"n": 0}
    stop = asyncio.Event()

    async def _flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        stop.set()  # second run: signal we recovered, let supervisor exit

    # _run_supervised(name, factory, stop_event, backoff=0) must catch
    # the exception, log, and re-run until stop_event is set.
    await es._run_supervised("flaky", _flaky, stop, backoff=0.0)
    assert calls["n"] == 2  # restarted after the crash, did not propagate


async def test_supervised_propagates_cancellation():
    stop = asyncio.Event()

    async def _hang():
        await asyncio.sleep(3600)

    task = asyncio.create_task(
        es._run_supervised("hang", _hang, stop, backoff=0.0))
    await asyncio.sleep(0)
    task.cancel()
    with __import__("pytest").raises(asyncio.CancelledError):
        await task


async def test_slow_sweep_does_not_block_monitor_tick(monkeypatch):
    """Make-or-break: the sweep runs in an executor; a slow sweep must
    NOT delay an event-loop coroutine tick (the monitor stream)."""
    monkeypatch.setattr(es, "_find_new_trigger",
                        AsyncMock(return_value=es.datetime.now(es.UTC)))

    def _slow_sweep():
        time.sleep(0.5)
        return 0
    monkeypatch.setattr(es, "_run_engine_sweep", _slow_sweep)

    ticked = []
    stop = asyncio.Event()

    async def _ticker():
        for _ in range(5):
            ticked.append(asyncio.get_event_loop().time())
            await asyncio.sleep(0.05)
        stop.set()

    pool = _Pool(None)
    await asyncio.gather(es._main_loop(pool, stop), _ticker())
    # 5 ticks ~0.05s apart finished well within the 0.5s blocking sweep
    assert len(ticked) == 5
    assert ticked[-1] - ticked[0] < 0.45


async def test_digest_trigger_fires_once_per_utc_day(monkeypatch):
    spawns = []

    async def _fake_exec(*args, **kw):
        spawns.append(args)
        class _P:
            returncode = 0
            async def wait(self): return 0
        return _P()
    monkeypatch.setattr(es.asyncio, "create_subprocess_exec", _fake_exec)

    state = {"last": None}
    d1 = date(2026, 5, 18)
    await es._maybe_fire_weekly_digest(state, today=d1)
    await es._maybe_fire_weekly_digest(state, today=d1)   # same day → no
    await es._maybe_fire_weekly_digest(state, today=date(2026, 5, 19))
    assert len(spawns) == 2
    # exact arg shape: (sys.executable, "-m", "ops.weekly_digest", "emit")
    assert spawns[0][:4] == (sys.executable, "-m", "ops.weekly_digest", "emit")
    assert spawns[1][:4] == (sys.executable, "-m", "ops.weekly_digest", "emit")


async def test_digest_trigger_crash_isolated(monkeypatch):
    async def _boom(*a, **k):
        raise OSError("spawn failed")
    monkeypatch.setattr(es.asyncio, "create_subprocess_exec", _boom)
    state = {"last": None}
    # must NOT raise — crash-isolated like _invoke_allocator
    await es._maybe_fire_weekly_digest(state, today=date(2026, 5, 18))
