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
    # The frozen SoT lives in ops/engine_supervisor (NOT engine_service).
    # Phase-0 shipped the crash classes; #243 Phase 1 adds the two
    # deterministic silent-absence detector classes.
    assert _esup.PLATFORM_SERVICE_FAILURE_CLASSES == frozenset(
        {"engine_service_task_crashloop", "engine_service_digest_failed",
         "engine_service_sweep_silent", "engine_service_digest_stalled"})
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
    >600s jump in the mocked monotonic clock between burst 1 and burst 2."""
    pool = _RecPool()
    stop = asyncio.Event()
    # 6 monotonic timestamps (floats): burst1 (3 in-window) then a >600s
    # gap then burst2 (3 in-window). time.monotonic is called once per crash.
    crash_mono = [
        0.0, 10.0, 20.0,
        10_000.0,   # >600s later → deque drains, latch resets
        10_010.0,
        10_020.0,
    ]
    seq = {"i": 0}
    calls = {"n": 0}

    async def _flaky():
        calls["n"] += 1
        if calls["n"] >= 7:
            stop.set()
            return
        raise RuntimeError(f"boom-{calls['n']}")

    def _fake_monotonic():
        t = crash_mono[min(seq["i"], len(crash_mono) - 1)]
        seq["i"] += 1
        return t

    await es._run_supervised("sweep", _flaky, stop, pool=pool,
                             backoff=0.0, _monotonic=_fake_monotonic)
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


# ---------------------------------------------------------------------------
# #243 Phase 1 (a): engine_service_sweep_silent — a qualifying trigger
# landed but no sweep ran within SWEEP_SILENT_SEC. Deterministic; consumes
# the EXISTING _find_new_trigger substrate (the green-repair SQL filter is
# NOT reimplemented). escalate-only, deterministic hold_id.
# ---------------------------------------------------------------------------


def _sweep_silent_hid():
    return _expected_hold_id("engine_service_sweep_silent", "sweep")


async def _run_one_poll(pool, *, find_new_trigger, monkeypatch,
                        run_sweep=lambda: 0, fixed_now=None):
    """Drive exactly ONE _main_loop iteration with injected seams, then
    stop. _find_new_trigger / _run_engine_sweep are monkeypatched; the
    weekly-digest check is neutralized (separate detector, Task 1.3)."""
    monkeypatch.setattr(es, "POLL_INTERVAL_SEC", 0)  # no real 60s sleep
    monkeypatch.setattr(es, "_find_new_trigger", find_new_trigger)
    monkeypatch.setattr(es, "_run_engine_sweep", run_sweep)
    monkeypatch.setattr(es, "_maybe_fire_weekly_digest",
                        AsyncMock(return_value=None))
    if fixed_now is not None:
        class _FixedDT(es.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now
        monkeypatch.setattr(es, "datetime", _FixedDT)
    stop = asyncio.Event()

    orig = es._find_new_trigger

    async def _wrapped(p, c):
        r = await orig(p, c)
        stop.set()  # one iteration only
        return r
    monkeypatch.setattr(es, "_find_new_trigger", _wrapped)
    await es._main_loop(pool, stop)


async def test_sweep_silent_bound_value():
    # 2*POLL_INTERVAL_SEC + 300 == 420; must exceed the longest
    # legitimate sweep so an in-flight long sweep is never flagged.
    assert es.SWEEP_SILENT_SEC == 2 * es.POLL_INTERVAL_SEC + 300
    assert isinstance(es.SWEEP_SILENT_SEC, int)


async def test_sweep_silent_fires_when_old_trigger_no_sweep(monkeypatch):
    """(i) qualifying trigger older than SWEEP_SILENT_SEC, no sweep ⇒
    exactly ONE ENGINE_ESCALATED, escalate-only, deterministic hold_id,
    payload-parity with the shipped Phase-0 emit."""
    pool = _RecPool()
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    old = now - timedelta(seconds=es.SWEEP_SILENT_SEC + 60)

    async def _ft(p, c):
        return old
    await _run_one_poll(pool, find_new_trigger=_ft, monkeypatch=monkeypatch,
                        fixed_now=now)
    rows = _escalated_rows(pool)
    assert len(rows) == 1, rows
    r = rows[0]
    assert r["engine"] == "engine_service:sweep"
    assert r["payload"]["failure_class"] == "engine_service_sweep_silent"
    assert r["payload"]["hold_id"] == _sweep_silent_hid()
    assert r["severity"] == "ERROR"
    # byte-parity with the shipped Phase-0 escalate-only payload shape
    assert set(r["payload"]) == {
        "schema", "hold_id", "engine", "failure_class",
        "reason", "attempts"}
    # escalate-only: NO ENGINE_HELD row for this hold_id
    held = [s for s, _ in pool.conn.execs
            if "INSERT INTO platform.application_log" in s
            and "ENGINE_HELD" in str(_)]
    assert held == []


async def test_sweep_silent_no_trigger_no_escalation(monkeypatch):
    """(ii) NO qualifying trigger (quiet weekend / no data-ops) ⇒ none."""
    pool = _RecPool()

    async def _ft(p, c):
        return None
    await _run_one_poll(pool, find_new_trigger=_ft, monkeypatch=monkeypatch)
    assert _escalated_rows(pool) == []


async def test_sweep_silent_sweep_ran_no_escalation(monkeypatch):
    """(iii) a sweep ran for the trigger ⇒ the cursor advanced past it,
    so _find_new_trigger returns None ⇒ no escalation (defers to the
    existing cursor signal, NOT a reimplemented predicate)."""
    pool = _RecPool()

    async def _ft(p, c):
        return None  # cursor already past it (sweep consumed it)
    await _run_one_poll(pool, find_new_trigger=_ft, monkeypatch=monkeypatch)
    assert _escalated_rows(pool) == []


async def test_sweep_silent_red_repair_no_escalation(monkeypatch):
    """(iv) a red (non-green) DATA_REPAIR_COMPLETE ⇒ no escalation —
    _find_new_trigger's SQL already excludes it (returns None); the
    check defers, it does NOT re-derive the trigger predicate."""
    pool = _RecPool()
    seen = {}

    async def _ft(p, c):
        seen["called"] = True
        return None  # green-only SQL filtered the red repair server-side
    await _run_one_poll(pool, find_new_trigger=_ft, monkeypatch=monkeypatch)
    assert seen.get("called") is True
    assert _escalated_rows(pool) == []


async def test_sweep_silent_young_trigger_no_escalation(monkeypatch):
    """(v) trigger younger than SWEEP_SILENT_SEC ⇒ no escalation
    (in-flight grace)."""
    pool = _RecPool()
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    young = now - timedelta(seconds=es.SWEEP_SILENT_SEC - 30)

    async def _ft(p, c):
        return young
    await _run_one_poll(pool, find_new_trigger=_ft, monkeypatch=monkeypatch,
                        fixed_now=now)
    assert _escalated_rows(pool) == []


async def test_sweep_silent_no_duplicate_on_repoll(monkeypatch):
    """(vi) re-poll after escalation ⇒ NO duplicate for the same
    trigger/hold_id."""
    pool = _RecPool()
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    old = now - timedelta(seconds=es.SWEEP_SILENT_SEC + 60)
    monkeypatch.setattr(es, "POLL_INTERVAL_SEC", 0)  # no real 60s sleep
    monkeypatch.setattr(es, "_run_engine_sweep", lambda: 0)
    monkeypatch.setattr(es, "_maybe_fire_weekly_digest",
                        AsyncMock(return_value=None))

    class _FixedDT(es.datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(es, "datetime", _FixedDT)

    polls = {"n": 0}
    stop = asyncio.Event()

    async def _ft(p, c):
        polls["n"] += 1
        if polls["n"] >= 3:
            stop.set()
        return old  # same trigger every poll (cursor never advances it)
    monkeypatch.setattr(es, "_find_new_trigger", _ft)
    await es._main_loop(pool, stop)
    rows = _escalated_rows(pool)
    assert len(rows) == 1, [r["payload"] for r in rows]


async def test_sweep_silent_emit_failure_does_not_break_loop(monkeypatch):
    """Crash-isolation: a failing escalate emit must not break the loop
    (reuses the Phase-0 _safe_emit_escalated wrapper)."""
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    old = now - timedelta(seconds=es.SWEEP_SILENT_SEC + 60)

    async def _boom_emit(*a, **k):
        raise RuntimeError("emit DB down")
    monkeypatch.setattr(es, "_emit_escalated", _boom_emit)

    async def _ft(p, c):
        return old
    # must NOT raise
    await _run_one_poll(_RecPool(), find_new_trigger=_ft,
                        monkeypatch=monkeypatch, fixed_now=now)


# ---------------------------------------------------------------------------
# #243 Phase 1 (c): engine_service_digest_stalled — the weekly digest was
# never reached/never advanced this trading ISO-week (distinct from the
# shipped engine_service_digest_failed rc≠0 path). Deterministic:
# is_trading_day guard + ISO-week scoping; no data-calendar re-derivation.
# ---------------------------------------------------------------------------


def _digest_stalled_hid():
    return _expected_hold_id("engine_service_digest_stalled",
                             "weekly_digest")


class _DigestConn:
    """fetchrow returns a 1-row marker iff a WEEKLY_DIGEST completion for
    the queried iso_week exists; else None."""

    def __init__(self, completed_weeks):
        self._weeks = set(completed_weeks)

    async def fetchrow(self, sql, *args):
        assert "iso_week" in sql, sql
        assert args[0] == "WEEKLY_DIGEST", args  # the DIGEST_EVENT param
        wk = args[-1]
        return {"ok": 1} if wk in self._weeks else None


class _DigestPool:
    def __init__(self, completed_weeks=()):
        self.conn = _RecConn()
        self._dc = _DigestConn(completed_weeks)

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield self  # both fetchrow (digest) + execute (escalate) here

    async def fetchrow(self, sql, *a):
        return await self._dc.fetchrow(sql, *a)

    async def execute(self, sql, *a):
        await self.conn.execute(sql, *a)


def _esc_rows_dp(pool):
    out = []
    for sql, args in pool.conn.execs:
        if "INSERT INTO platform.application_log" not in sql:
            continue
        engine, _rid, etype, sev, msg, data_json = args
        if etype != _esup.ESCALATED_EVENT:
            continue
        out.append({"engine": engine, "severity": sev,
                    "payload": __import__("json").loads(data_json)})
    return out


# Wed 2026-05-20 12:00 UTC — a trading day, ISO week 2026-W21; the week's
# Monday 00:00 UTC rollover is >> DIGEST_STALE_SEC in the past.
_WED = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
_ISO_WK_WED = "2026-W21"


async def test_digest_stale_bound_value():
    assert es.DIGEST_STALE_SEC == 21600  # ~6h
    assert isinstance(es.DIGEST_STALE_SEC, int)


async def test_digest_stalled_fires_trading_day_overdue_no_completion(
        monkeypatch):
    """(i) trading day + overdue + no completion ⇒ ONE ENGINE_ESCALATED
    (digest_stalled, weekly_digest hold_id, escalate-only)."""
    monkeypatch.setattr(es, "is_trading_day", lambda _dt: True)
    pool = _DigestPool(completed_weeks=())
    await es._maybe_escalate_digest_stalled(pool, _WED, set())
    rows = _esc_rows_dp(pool)
    assert len(rows) == 1, rows
    r = rows[0]
    assert r["engine"] == "engine_service:weekly_digest"
    assert r["payload"]["failure_class"] == "engine_service_digest_stalled"
    assert r["payload"]["hold_id"] == _digest_stalled_hid()
    assert r["severity"] == "ERROR"
    assert set(r["payload"]) == {
        "schema", "hold_id", "engine", "failure_class",
        "reason", "attempts"}


async def test_digest_stalled_not_trading_day_no_escalation(monkeypatch):
    """(ii) is_trading_day False (weekend/holiday) ⇒ no escalation."""
    monkeypatch.setattr(es, "is_trading_day", lambda _dt: False)
    pool = _DigestPool(completed_weeks=())
    await es._maybe_escalate_digest_stalled(pool, _WED, set())
    assert _esc_rows_dp(pool) == []


async def test_digest_stalled_already_emitted_no_escalation(monkeypatch):
    """(iii) digest already emitted this ISO-week ⇒ no escalation."""
    monkeypatch.setattr(es, "is_trading_day", lambda _dt: True)
    pool = _DigestPool(completed_weeks={_ISO_WK_WED})
    await es._maybe_escalate_digest_stalled(pool, _WED, set())
    assert _esc_rows_dp(pool) == []


async def test_digest_stalled_within_grace_no_escalation(monkeypatch):
    """(iv) within DIGEST_STALE_SEC grace ⇒ no escalation. Monday
    2026-05-18 just after week-start rollover (< 6h elapsed)."""
    monkeypatch.setattr(es, "is_trading_day", lambda _dt: True)
    just_after = datetime(2026, 5, 18, 3, 0, tzinfo=UTC)  # Mon, ~3h in
    pool = _DigestPool(completed_weeks=())
    await es._maybe_escalate_digest_stalled(pool, just_after, set())
    assert _esc_rows_dp(pool) == []


async def test_digest_stalled_distinct_from_digest_failed(monkeypatch):
    """(v) a FAILED digest (rc≠0) still uses the SHIPPED
    engine_service_digest_failed class/path — _maybe_fire_weekly_digest
    is unchanged; this detector only covers the never-reached case."""
    async def _fake_exec(*a, **k):
        class _P:
            async def wait(self): return 7
        return _P()
    monkeypatch.setattr(es.asyncio, "create_subprocess_exec", _fake_exec)
    pool = _RecPool()
    await es._maybe_fire_weekly_digest({"last": None}, pool=pool,
                                       today=date(2026, 5, 20))
    rows = _escalated_rows(pool)
    assert len(rows) == 1
    assert rows[0]["payload"]["failure_class"] == \
        "engine_service_digest_failed"  # NOT _stalled


async def test_digest_stalled_no_duplicate_on_repoll(monkeypatch):
    """(vi) re-poll ⇒ no duplicate (one-shot per ISO-week)."""
    monkeypatch.setattr(es, "is_trading_day", lambda _dt: True)
    pool = _DigestPool(completed_weeks=())
    emitted: set[str] = set()  # the loop-local one-shot dedup set
    await es._maybe_escalate_digest_stalled(pool, _WED, emitted)
    await es._maybe_escalate_digest_stalled(pool, _WED, emitted)
    await es._maybe_escalate_digest_stalled(pool, _WED, emitted)
    assert len(_esc_rows_dp(pool)) == 1


async def test_digest_stalled_emit_failure_does_not_raise(monkeypatch):
    """Crash-isolation: a failing escalate emit must not propagate."""
    monkeypatch.setattr(es, "is_trading_day", lambda _dt: True)

    async def _boom_emit(*a, **k):
        raise RuntimeError("emit DB down")
    monkeypatch.setattr(es, "_emit_escalated", _boom_emit)
    # must NOT raise
    await es._maybe_escalate_digest_stalled(
        _DigestPool(completed_weeks=()), _WED, set())


async def test_digest_stalled_wired_into_main_loop(monkeypatch):
    """The detector is invoked from _main_loop adjacent to the
    _maybe_fire_weekly_digest call (deterministic, crash-isolated)."""
    monkeypatch.setattr(es, "POLL_INTERVAL_SEC", 0)
    monkeypatch.setattr(es, "_run_engine_sweep", lambda: 0)
    monkeypatch.setattr(es, "_maybe_fire_weekly_digest",
                        AsyncMock(return_value=None))
    seen = {}

    async def _spy(pool, now, emitted):
        seen["called"] = True
    monkeypatch.setattr(es, "_maybe_escalate_digest_stalled", _spy)
    stop = asyncio.Event()

    async def _ft(p, c):
        stop.set()
        return None
    monkeypatch.setattr(es, "_find_new_trigger", _ft)
    await es._main_loop(_RecPool(), stop)
    assert seen.get("called") is True


def test_run_engine_service_wrapper_has_env_for_monitor():
    """H-2: the consolidated daemon's wrapper must source .env (so the
    co-hosted TradeMonitor sees ALPACA_KEY/ALPACA_SECRET) AND keep the
    IPv4-pooler pin (launchd network-namespace requirement)."""
    sh = (REPO_ROOT / "scripts" / "run_engine_service.sh").read_text()
    assert "source .env" in sh, "wrapper must source .env for ALPACA creds"
    assert 'DATABASE_URL="${DATABASE_URL_IPV4:-$DATABASE_URL}"' in sh
    assert "-m ops.engine_service" in sh
