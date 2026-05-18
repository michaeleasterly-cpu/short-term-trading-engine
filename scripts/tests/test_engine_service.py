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


# ---------------------------------------------------------------------------
# Phase-0: engine-daemon co-hosted platform-service failures escalate into
# the engine Ladder via ENGINE_ESCALATED (no LLM; deterministic).
# ---------------------------------------------------------------------------

import hashlib  # noqa: E402

from ops import engine_supervisor as _esup  # noqa: E402


class _RecConn:
    def __init__(self):
        self.execs = []

    async def execute(self, sql, *args):
        self.execs.append((sql, args))


class _RecPool:
    def __init__(self):
        self.conn = _RecConn()

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield self.conn


def _escalated_rows(pool):
    """Decode the ENGINE_ESCALATED application_log INSERTs (sql, args)."""
    out = []
    for sql, args in pool.conn.execs:
        if "INSERT INTO platform.application_log" not in sql:
            continue
        engine, _run_id, event_type, severity, message, data_json = args
        if event_type != _esup.ESCALATED_EVENT:
            continue
        out.append({
            "engine": engine, "event_type": event_type,
            "severity": severity, "message": message,
            "payload": __import__("json").loads(data_json),
        })
    return out


def test_platform_service_failure_classes_constant():
    # The frozen SoT lives in ops/engine_supervisor (NOT engine_service)
    assert _esup.PLATFORM_SERVICE_FAILURE_CLASSES == frozenset(
        {"engine_service_task_crashloop", "engine_service_digest_failed"})
    # NOT folded into the DA-1 infra set
    assert (_esup.PLATFORM_SERVICE_FAILURE_CLASSES
            & _esup.INFRA_FAILURE_CLASSES) == set()
    # the engine_service module uses the matching class-name literals
    assert es._CRASHLOOP_CLASS in _esup.PLATFORM_SERVICE_FAILURE_CLASSES
    assert es._DIGEST_FAILED_CLASS in _esup.PLATFORM_SERVICE_FAILURE_CLASSES


def test_emit_escalated_importable_without_engine_service():
    """No-cycle sanity: engine_supervisor._emit_escalated imports clean
    with no ops.engine_service in sys.modules (acyclic)."""
    import importlib
    saved = {k: v for k, v in sys.modules.items()
             if k == "ops.engine_service"}
    for k in list(saved):
        del sys.modules[k]
    try:
        mod = importlib.import_module("ops.engine_supervisor")
        assert callable(mod._emit_escalated)
        assert "ops.engine_service" not in sys.modules
    finally:
        sys.modules.update(saved)


def _expected_hold_id(failure_class, task_name):
    return "engsvc-" + hashlib.sha256(
        f"{failure_class}|{task_name}".encode()).hexdigest()[:16]


async def test_crashloop_under_budget_no_escalation():
    """2 in-window crashes ⇒ NO ENGINE_ESCALATED (still logs+restarts)."""
    pool = _RecPool()
    stop = asyncio.Event()
    calls = {"n": 0}

    async def _flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("boom")
        stop.set()  # 3rd run recovers, supervisor exits cleanly

    await es._run_supervised("sweep", _flaky, stop, pool=pool, backoff=0.0)
    assert calls["n"] == 3
    assert _escalated_rows(pool) == []


async def test_crashloop_emits_once_on_third_crash_then_no_dup():
    """3 crashes within 600s ⇒ exactly ONE ENGINE_ESCALATED with the
    frozen class/hold_id/engine/payload; a 4th in-window ⇒ no dup."""
    pool = _RecPool()
    stop = asyncio.Event()
    calls = {"n": 0}

    async def _flaky():
        calls["n"] += 1
        if calls["n"] >= 5:
            stop.set()
            return
        raise RuntimeError(f"boom-{calls['n']}")

    await es._run_supervised("sweep", _flaky, stop, pool=pool, backoff=0.0)
    rows = _escalated_rows(pool)
    assert len(rows) == 1, rows
    r = rows[0]
    assert r["engine"] == "engine_service:sweep"
    fc = "engine_service_task_crashloop"
    assert r["payload"]["failure_class"] == fc
    expected_hid = _expected_hold_id(fc, "sweep")
    assert r["payload"]["hold_id"] == expected_hid
    assert r["payload"]["attempts"] == 3
    # byte-parity with engine_supervisor._emit_escalated payload shape
    assert set(r["payload"]) == {
        "schema", "hold_id", "engine", "failure_class",
        "reason", "attempts"}
    assert r["payload"]["engine"] == "engine_service:sweep"
    assert r["severity"] == "ERROR"


async def test_crashloop_re_escalates_after_recovery():
    """After the rolling window drains (recovery), a fresh 3x in-window
    ⇒ a SECOND ENGINE_ESCALATED (the ``escalated`` latch reset). The
    co-task ONLY ever crashes (never cleanly returns — a clean return
    would exit _run_supervised); the gap that drains the deque is a
    >600s jump in the mocked crash clock between burst 1 and burst 2."""
    pool = _RecPool()
    stop = asyncio.Event()
    base = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    # 6 crash timestamps: burst1 (3 in-window) then a >600s jump then
    # burst2 (3 in-window). datetime.now is called once per crash.
    crash_times = [
        base, base + timedelta(seconds=10), base + timedelta(seconds=20),
        base + timedelta(hours=3),  # >600s later → deque drains, latch off
        base + timedelta(hours=3, seconds=10),
        base + timedelta(hours=3, seconds=20),
    ]
    seq = {"i": 0}
    calls = {"n": 0}

    async def _flaky():
        calls["n"] += 1
        if calls["n"] >= 7:
            stop.set()
            return
        raise RuntimeError(f"boom-{calls['n']}")

    real_dt = datetime

    class _DT:
        @staticmethod
        def now(tz=None):
            t = crash_times[min(seq["i"], len(crash_times) - 1)]
            seq["i"] += 1
            return t

        def __new__(cls, *a, **k):
            return real_dt(*a, **k)

    with patch.object(es, "datetime", _DT):
        await es._run_supervised("sweep", _flaky, stop, pool=pool,
                                 backoff=0.0)
    rows = _escalated_rows(pool)
    assert len(rows) == 2, [r["payload"] for r in rows]
    assert all(r["payload"]["failure_class"]
               == "engine_service_task_crashloop" for r in rows)
    # both use the SAME deterministic hold_id (same class+task)
    assert {r["payload"]["hold_id"] for r in rows} == {
        _expected_hold_id("engine_service_task_crashloop", "sweep")}


async def test_crashloop_emit_failure_does_not_kill_task(monkeypatch):
    """An _emit_escalated raising must NOT propagate out of
    _run_supervised (the one-crashed-co-task invariant holds)."""
    stop = asyncio.Event()
    calls = {"n": 0}

    async def _boom_emit(*a, **k):
        raise RuntimeError("emit DB down")
    monkeypatch.setattr(es, "_emit_escalated", _boom_emit)

    async def _flaky():
        calls["n"] += 1
        if calls["n"] >= 6:
            stop.set()
            return
        raise RuntimeError("boom")

    # must complete (stop set) without raising despite emit failing
    await es._run_supervised("sweep", _flaky, stop,
                             pool=_RecPool(), backoff=0.0)
    assert calls["n"] == 6


async def test_digest_spawn_exception_emits_escalated(monkeypatch):
    async def _boom(*a, **k):
        raise OSError("spawn failed")
    monkeypatch.setattr(es.asyncio, "create_subprocess_exec", _boom)
    pool = _RecPool()
    state = {"last": None}
    await es._maybe_fire_weekly_digest(state, pool=pool,
                                       today=date(2026, 5, 18))
    rows = _escalated_rows(pool)
    assert len(rows) == 1
    r = rows[0]
    assert r["engine"] == "engine_service:weekly_digest"
    assert r["payload"]["failure_class"] == "engine_service_digest_failed"
    assert r["payload"]["hold_id"] == _expected_hold_id(
        "engine_service_digest_failed", "weekly_digest")
    assert r["payload"]["attempts"] == 1


async def test_digest_nonzero_rc_emits_escalated(monkeypatch):
    async def _fake_exec(*a, **k):
        class _P:
            async def wait(self): return 3
        return _P()
    monkeypatch.setattr(es.asyncio, "create_subprocess_exec", _fake_exec)
    pool = _RecPool()
    await es._maybe_fire_weekly_digest({"last": None}, pool=pool,
                                       today=date(2026, 5, 18))
    rows = _escalated_rows(pool)
    assert len(rows) == 1
    assert rows[0]["payload"]["failure_class"] == "engine_service_digest_failed"
    assert "rc=3" in rows[0]["payload"]["reason"]


async def test_digest_success_emits_nothing(monkeypatch):
    async def _fake_exec(*a, **k):
        class _P:
            async def wait(self): return 0
        return _P()
    monkeypatch.setattr(es.asyncio, "create_subprocess_exec", _fake_exec)
    pool = _RecPool()
    await es._maybe_fire_weekly_digest({"last": None}, pool=pool,
                                       today=date(2026, 5, 18))
    assert _escalated_rows(pool) == []


async def test_digest_never_raises_even_if_emit_fails(monkeypatch):
    async def _boom(*a, **k):
        raise OSError("spawn failed")
    monkeypatch.setattr(es.asyncio, "create_subprocess_exec", _boom)

    async def _boom_emit(*a, **k):
        raise RuntimeError("emit DB down")
    monkeypatch.setattr(es, "_emit_escalated", _boom_emit)
    # must NOT raise
    await es._maybe_fire_weekly_digest({"last": None}, pool=_RecPool(),
                                       today=date(2026, 5, 18))


def test_run_engine_service_wrapper_has_env_for_monitor():
    """H-2: the consolidated daemon's wrapper must source .env (so the
    co-hosted TradeMonitor sees ALPACA_KEY/ALPACA_SECRET) AND keep the
    IPv4-pooler pin (launchd network-namespace requirement)."""
    sh = (REPO_ROOT / "scripts" / "run_engine_service.sh").read_text()
    assert "source .env" in sh, "wrapper must source .env for ALPACA creds"
    assert 'DATABASE_URL="${DATABASE_URL_IPV4:-$DATABASE_URL}"' in sh
    assert "-m ops.engine_service" in sh
