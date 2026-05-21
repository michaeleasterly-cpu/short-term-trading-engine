"""Unit tests for ops/llm_triage_service.py — the event-driven LLM-triage
daemon (LT-P3 4d + 2026-05-21 autonomous-data flip).

Structural sibling of tests/test_data_repair_service.py: a fake asyncpg
pool (no DB), the data-lane ``run_autonomous_recovery`` monkeypatched
(no LLM, no real subprocess), and the cursor-advance behaviour asserted
directly via ``_main_loop`` / ``_find_new_trigger``.

Coverage:
  (i)   no trigger event           → recovery NOT called, cursor unchanged
  (ii)  DATA_REPAIR_ESCALATED      → recovery called once, cursor advances
  (iii) recovery raising           → daemon loop survives (no crash), logged
  (iv)  DATA_SOURCE_ESCALATED      → also triggers (autonomous)
  (iv') INGESTION_AUTO_RECOVERY_FAILED → also triggers (autonomous, new)
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# NOTE: ``scripts/ops.py`` is a single-file module that some other test
# (tpcore/tests/test_ops_helpers.py) imports as the top-level name
# ``ops`` after putting ``scripts/`` on sys.path — shadowing the ``ops/``
# *package* in sys.modules. So ``import ops.llm_triage_service`` is
# collection-order fragile; we load the module by file path under a
# private, collision-free name (mirrors test_data_repair_service).
#
# UNLIKE ops/data_repair_service.py (imports only ``tpcore.*`` → a bare
# file-load is safe), ops/llm_triage_service.py does ``from
# ops.llm_data_recovery import run_autonomous_recovery`` (2026-05-21
# autonomous-data flip) + ``from ops.engine_llm_triage import
# run_triage`` + ``from ops.llm_lab_emitter import ...`` at module
# top-level. Those intra-package imports fail ("'ops' is not a
# package") whenever the scripts single-file ``ops`` is shadowing
# sys.modules. We must NOT mutate the shared sys.modules['ops']
# permanently (breaks test_ops_helpers when this module collects
# between it and scripts/tests). Instead: snapshot the relevant
# sys.modules entries, install minimal collision-free stubs (every test
# here monkeypatches the bound callables so the real impls are NEVER
# exercised), exec the daemon by file path, then RESTORE sys.modules
# exactly — zero global side effects, collection-order safe.
_LTS_PATH = Path(__file__).resolve().parent.parent / "ops" / "llm_triage_service.py"
_SAVED = {
    k: sys.modules.get(k)
    for k in (
        "ops",
        "ops.llm_data_recovery",
        "ops.engine_llm_triage",
        "ops.llm_lab_emitter",
    )
}
try:
    _ops = sys.modules.get("ops")
    if not isinstance(getattr(_ops, "__path__", None), list):
        _pkg = types.ModuleType("ops")
        _pkg.__path__ = [str(_LTS_PATH.parent)]  # make it package-shaped
        sys.modules["ops"] = _pkg

    _rstub = types.ModuleType("ops.llm_data_recovery")

    async def _stub_run_autonomous_recovery(
        *_a, **_k
    ):  # pragma: no cover - replaced
        raise AssertionError(
            "run_autonomous_recovery must be monkeypatched in this test"
        )

    _rstub.run_autonomous_recovery = _stub_run_autonomous_recovery
    _rstub.AUTONOMOUS_DATA_TRIGGER_EVENT_TYPES = (
        "DATA_REPAIR_ESCALATED",
        "DATA_SOURCE_ESCALATED",
        "INGESTION_AUTO_RECOVERY_FAILED",
    )
    sys.modules["ops.llm_data_recovery"] = _rstub

    _estub = types.ModuleType("ops.engine_llm_triage")

    async def _stub_engine_run_triage(*_a, **_k):  # pragma: no cover - replaced
        raise AssertionError(
            "engine_run_triage must be monkeypatched in this test"
        )

    _estub.run_triage = _stub_engine_run_triage
    sys.modules["ops.engine_llm_triage"] = _estub

    _lestub = types.ModuleType("ops.llm_lab_emitter")

    async def _stub_lab_emitter(*_a, **_k):  # pragma: no cover - replaced
        raise AssertionError(
            "run_lab_emitter_cotask must be monkeypatched in this test"
        )

    _lestub.run_lab_emitter_cotask = _stub_lab_emitter
    _lestub.LAB_EMITTER_TRIGGER_EVENT_TYPES = ()
    sys.modules["ops.llm_lab_emitter"] = _lestub

    _spec = importlib.util.spec_from_file_location("_lts_under_test", _LTS_PATH)
    assert _spec is not None and _spec.loader is not None
    lts = importlib.util.module_from_spec(_spec)
    sys.modules["_lts_under_test"] = lts
    _spec.loader.exec_module(lts)
finally:
    # Restore sys.modules entries EXACTLY so no later-collected test
    # (e.g. test_ops_helpers) sees our scaffolding.
    for _k, _v in _SAVED.items():
        if _v is None:
            sys.modules.pop(_k, None)
        else:
            sys.modules[_k] = _v


# ────────────────────────────────────────────────────────────────────────
# Fakes (mirror tests/test_data_repair_service.py).
# ────────────────────────────────────────────────────────────────────────


# pytest-xdist: pin this ops-shadow module to one worker so its
# sys.modules['ops'] / scripts/ops.py loading stays single-process
# (the ops/ package-shadow is a single-process invariant). P1.3.
pytestmark = pytest.mark.xdist_group("ops_shadow")


class _Conn:
    def __init__(self, pool: _Pool) -> None:
        self._pool = pool

    async def fetchrow(self, sql: str, *args):
        # _find_new_trigger: newest trigger with recorded_at > cursor.
        # args[0] is the event-type list the lane passed (data lane:
        # TRIGGER_EVENT_TYPES; engine lane: ENGINE_TRIGGER_EVENT_TYPES) —
        # filter on it so the ONE fake serves BOTH co-tasks.
        if "ORDER BY recorded_at DESC" in sql:
            event_types = set(args[0])
            cursor = args[1]
            hits = [
                ts
                for ts, et in self._pool.events
                if et in event_types and ts > cursor
            ]
            if not hits:
                return None
            return {"recorded_at": max(hits)}
        raise AssertionError(f"unexpected fetchrow SQL: {sql!r}")


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _Pool:
    def __init__(self, events: list[tuple[datetime, str]] | None = None) -> None:
        self.events = events or []

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(self))


@pytest.fixture
def lock_dir(tmp_path) -> str:
    """Overridable lock dir so tests never touch a real lock path."""
    return os.path.join(str(tmp_path), "ste-llm-triage-service.lock")


async def _run_one_loop(
    monkeypatch, pool, *, raises: bool = False, lock_dir: str | None = None
) -> list:
    """Drive _main_loop for exactly one poll then signal stop. Returns
    the list of run_autonomous_recovery call markers."""
    calls: list = []

    async def fake_recovery(p):
        calls.append(p)
        if raises:
            raise RuntimeError("recovery boom")
        return None

    monkeypatch.setattr(lts, "run_autonomous_recovery", fake_recovery)

    stop_event = asyncio.Event()
    # POLL_INTERVAL_SEC is large; we stop right after the first poll by
    # patching the sleep to set the stop event.
    real_wait_for = asyncio.wait_for

    async def stop_after_first(coro, timeout):  # noqa: ANN001
        stop_event.set()
        coro.close()
        return None

    monkeypatch.setattr(lts.asyncio, "wait_for", stop_after_first)
    ld = lock_dir if lock_dir is not None else lts.DEFAULT_LOCK_DIR
    try:
        await lts._main_loop(pool, stop_event, ld)  # noqa: SLF001
    finally:
        monkeypatch.setattr(lts.asyncio, "wait_for", real_wait_for)
    return calls


# ────────────────────────────────────────────────────────────────────────
# (i) No trigger event → run_triage NOT called, cursor unchanged.
# ────────────────────────────────────────────────────────────────────────


async def test_no_trigger_does_not_call_run_triage(monkeypatch) -> None:
    pool = _Pool(events=[])  # nothing on the bus
    calls = await _run_one_loop(monkeypatch, pool)
    assert calls == []  # run_triage NEVER called without a trigger


async def test_no_trigger_cursor_unchanged(monkeypatch) -> None:
    # An OLD event (before the initial cursor lookback) must not fire.
    old = datetime.now(UTC) - timedelta(hours=6)
    pool = _Pool(events=[(old, "DATA_REPAIR_ESCALATED")])
    calls = await _run_one_loop(monkeypatch, pool)
    assert calls == []  # event predates cursor → no trigger


# ────────────────────────────────────────────────────────────────────────
# (ii) DATA_REPAIR_ESCALATED newer than cursor → run_triage once.
# ────────────────────────────────────────────────────────────────────────


async def test_repair_escalated_triggers_run_triage_once(
    monkeypatch, lock_dir
) -> None:
    recent = datetime.now(UTC) - timedelta(minutes=1)
    pool = _Pool(events=[(recent, "DATA_REPAIR_ESCALATED")])
    calls = await _run_one_loop(monkeypatch, pool, lock_dir=lock_dir)
    assert len(calls) == 1  # fired exactly once
    assert calls[0] is pool  # run_triage(pool) — the shared pool
    assert not os.path.exists(lock_dir)  # released after the pass


async def test_find_new_trigger_advances_cursor(monkeypatch) -> None:
    recent = datetime.now(UTC) - timedelta(minutes=1)
    pool = _Pool(events=[(recent, "DATA_REPAIR_ESCALATED")])
    cursor = datetime.now(UTC) - timedelta(hours=1)
    newest = await lts._find_new_trigger(pool, cursor)  # noqa: SLF001
    assert newest == recent  # the daemon advances its cursor to this
    # After advancing, the same event no longer re-triggers.
    assert await lts._find_new_trigger(pool, recent) is None  # noqa: SLF001


# ────────────────────────────────────────────────────────────────────────
# (iii) run_triage raising → daemon loop survives (does not crash).
# ────────────────────────────────────────────────────────────────────────


async def test_run_triage_exception_does_not_crash_loop(
    monkeypatch, lock_dir
) -> None:
    recent = datetime.now(UTC) - timedelta(minutes=1)
    pool = _Pool(events=[(recent, "DATA_SOURCE_ESCALATED")])
    # raises=True → fake_run_triage raises RuntimeError. The loop must
    # complete normally (no propagation) — crash-isolated, advisory.
    calls = await _run_one_loop(
        monkeypatch, pool, raises=True, lock_dir=lock_dir
    )
    assert len(calls) == 1  # it WAS called (and raised) — but loop survived
    # Lock released even though run_triage raised (finally path).
    assert not os.path.exists(lock_dir)


# ────────────────────────────────────────────────────────────────────────
# (iv) DATA_SOURCE_ESCALATED also triggers.
# ────────────────────────────────────────────────────────────────────────


async def test_data_source_escalated_also_triggers(
    monkeypatch, lock_dir
) -> None:
    recent = datetime.now(UTC) - timedelta(minutes=1)
    pool = _Pool(events=[(recent, "DATA_SOURCE_ESCALATED")])
    calls = await _run_one_loop(monkeypatch, pool, lock_dir=lock_dir)
    assert len(calls) == 1  # DATA_SOURCE_ESCALATED is a trigger too


# ────────────────────────────────────────────────────────────────────────
# (v) Sibling-parity self-exclusion lock (mirrors
#     tests/test_data_repair_service.py's lock-contention test).
# ────────────────────────────────────────────────────────────────────────


async def test_lock_held_by_live_pid_skips_triage(
    monkeypatch, lock_dir
) -> None:
    """A concurrent invocation (lock held by a LIVE pid) → this pass
    SKIPS run_triage entirely; the held lock is left untouched."""
    recent = datetime.now(UTC) - timedelta(minutes=1)
    pool = _Pool(events=[(recent, "DATA_REPAIR_ESCALATED")])

    # Hold the lock with THIS (alive) process's pid (simulating the
    # "other" concurrent invocation).
    os.mkdir(lock_dir)
    with open(os.path.join(lock_dir, "pid"), "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))

    calls = await _run_one_loop(monkeypatch, pool, lock_dir=lock_dir)

    assert calls == []  # run_triage NOT called — lock held
    assert os.path.exists(lock_dir)  # the other process's lock untouched


async def test_dead_pid_lock_is_reclaimed(monkeypatch, lock_dir) -> None:
    """A stale lock dir whose pid is dead is reclaimed — triage runs."""
    recent = datetime.now(UTC) - timedelta(minutes=1)
    pool = _Pool(events=[(recent, "DATA_REPAIR_ESCALATED")])

    os.mkdir(lock_dir)
    with open(os.path.join(lock_dir, "pid"), "w", encoding="utf-8") as fh:
        fh.write(str(2**31 - 1))  # PID that won't exist

    calls = await _run_one_loop(monkeypatch, pool, lock_dir=lock_dir)

    assert len(calls) == 1  # reclaimed + ran, not skipped
    assert not os.path.exists(lock_dir)  # released after the pass


def test_trigger_event_types_are_the_three_autonomous_escalations() -> None:
    # Structural: the daemon triggers on autonomous data-lane
    # escalations only. 2026-05-21: the in-orchestrator-cascade
    # exhaustion class INGESTION_AUTO_RECOVERY_FAILED is now part of the
    # set — the autonomous data-recovery handler picks it up so the
    # chain (orchestrator cascade → smart-feed cascade → llm_triage
    # autonomous action) closes without operator intervention.
    assert lts.TRIGGER_EVENT_TYPES == (
        "DATA_REPAIR_ESCALATED",
        "DATA_SOURCE_ESCALATED",
        "INGESTION_AUTO_RECOVERY_FAILED",
    )


# ────────────────────────────────────────────────────────────────────────
# (vi) Startup `git worktree prune` — invoked ONCE before any work, and
#      crash-isolated (a git failure must NOT abort the daemon; it
#      proceeds into the poll loop). No real git ever runs (subprocess
#      is monkeypatched).
# ────────────────────────────────────────────────────────────────────────


async def test_startup_worktree_prune_invoked_once(monkeypatch) -> None:
    calls: list = []

    def fake_run(args, **kwargs):  # noqa: ANN001
        calls.append((args, kwargs))

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    monkeypatch.setattr(lts.subprocess, "run", fake_run)
    pool = _Pool(events=[])  # no trigger — loop runs one poll then stops
    await _run_one_loop(monkeypatch, pool)

    prune_calls = [
        c for c in calls if c[0][:3] == ["git", "worktree", "prune"]
    ]
    assert len(prune_calls) == 1  # exactly once at startup
    # no shell, list-args, cwd = repo root
    assert prune_calls[0][1].get("shell") in (None, False)
    assert str(prune_calls[0][1]["cwd"]) == str(lts._REPO_ROOT)  # noqa: SLF001


async def test_startup_worktree_prune_failure_does_not_crash_daemon(
    monkeypatch,
) -> None:
    def boom(*_a, **_k):
        # Simulate git absent / non-zero / timeout at startup.
        raise FileNotFoundError("[Errno 2] No such file or directory: 'git'")

    monkeypatch.setattr(lts.subprocess, "run", boom)
    pool = _Pool(events=[])
    # The loop must complete normally (one poll, then stop) DESPITE the
    # startup git failure — crash-isolated, never aborts startup.
    calls = await _run_one_loop(monkeypatch, pool)
    assert calls == []  # reached the poll loop; no run_triage (no trigger)


def test_startup_prune_helper_is_crash_isolated_standalone(monkeypatch) -> None:
    # Direct unit: _startup_worktree_prune NEVER raises, whatever git does.
    def boom(*_a, **_k):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(lts.subprocess, "run", boom)
    lts._startup_worktree_prune()  # must NOT raise  # noqa: SLF001


# ════════════════════════════════════════════════════════════════════════
# Phase 3 (B1) — the daemon co-hosts BOTH lanes' triage loops as two
# independent _run_supervised co-tasks on the ONE advisory pool. The
# engine co-task cursor-polls ENGINE_ESCALATED and fires
# ops.engine_llm_triage.run_triage; it is crash-isolated from the data
# co-task (and vice-versa); the lock + startup-prune idioms are reused
# verbatim. test_two_daemon_invariant.py MUST stay green UNEDITED
# (B1 placement: no installer/label/whitelist change).
# ════════════════════════════════════════════════════════════════════════


async def _run_one_engine_loop(
    monkeypatch, pool, *, raises: bool = False, lock_dir: str | None = None
) -> list:
    """Drive the ENGINE lane loop for exactly one poll then stop.
    Mirrors _run_one_loop but patches the engine triage callable."""
    calls: list = []

    async def fake_engine_run_triage(p):
        calls.append(p)
        if raises:
            raise RuntimeError("engine triage boom")
        return None

    monkeypatch.setattr(lts, "engine_run_triage", fake_engine_run_triage)

    stop_event = asyncio.Event()

    async def stop_after_first(coro, timeout):  # noqa: ANN001
        stop_event.set()
        coro.close()
        return None

    real_wait_for = asyncio.wait_for
    monkeypatch.setattr(lts.asyncio, "wait_for", stop_after_first)
    ld = lock_dir if lock_dir is not None else lts.DEFAULT_LOCK_DIR
    try:
        await lts._engine_loop(pool, stop_event, ld)  # noqa: SLF001
    finally:
        monkeypatch.setattr(lts.asyncio, "wait_for", real_wait_for)
    return calls


def test_engine_trigger_event_types_is_engine_escalated() -> None:
    # Structural: the engine co-task triggers on the engine-lane
    # ENGINE_ESCALATED escalation (the deterministic-Phase-0 surface).
    assert lts.ENGINE_TRIGGER_EVENT_TYPES == ("ENGINE_ESCALATED",)


async def test_engine_escalated_triggers_engine_run_triage_once(
    monkeypatch, lock_dir
) -> None:
    recent = datetime.now(UTC) - timedelta(minutes=1)
    pool = _Pool(events=[(recent, "ENGINE_ESCALATED")])
    calls = await _run_one_engine_loop(monkeypatch, pool, lock_dir=lock_dir)
    assert len(calls) == 1  # engine triage fired exactly once
    assert calls[0] is pool  # run_triage(pool) — the shared advisory pool
    assert not os.path.exists(lock_dir)  # lock released after the pass


async def test_engine_loop_ignores_data_lane_events(
    monkeypatch, lock_dir
) -> None:
    # A data-lane escalation must NOT trigger the engine co-task.
    recent = datetime.now(UTC) - timedelta(minutes=1)
    pool = _Pool(events=[(recent, "DATA_REPAIR_ESCALATED")])
    calls = await _run_one_engine_loop(monkeypatch, pool, lock_dir=lock_dir)
    assert calls == []  # engine lane only consumes ENGINE_ESCALATED


async def test_data_loop_ignores_engine_lane_events(monkeypatch) -> None:
    # Symmetry: an ENGINE_ESCALATED must NOT trigger the data co-task.
    recent = datetime.now(UTC) - timedelta(minutes=1)
    pool = _Pool(events=[(recent, "ENGINE_ESCALATED")])
    calls = await _run_one_loop(monkeypatch, pool)
    assert calls == []  # data lane only consumes its two escalations


async def test_engine_find_new_trigger_advances_cursor(monkeypatch) -> None:
    recent = datetime.now(UTC) - timedelta(minutes=1)
    pool = _Pool(events=[(recent, "ENGINE_ESCALATED")])
    cursor = datetime.now(UTC) - timedelta(hours=1)
    newest = await lts._find_new_trigger(  # noqa: SLF001
        pool, cursor, lts.ENGINE_TRIGGER_EVENT_TYPES
    )
    assert newest == recent
    # After advancing, the same event no longer re-triggers.
    assert (
        await lts._find_new_trigger(  # noqa: SLF001
            pool, recent, lts.ENGINE_TRIGGER_EVENT_TYPES
        )
        is None
    )


async def test_engine_run_triage_exception_does_not_crash_loop(
    monkeypatch, lock_dir
) -> None:
    recent = datetime.now(UTC) - timedelta(minutes=1)
    pool = _Pool(events=[(recent, "ENGINE_ESCALATED")])
    calls = await _run_one_engine_loop(
        monkeypatch, pool, raises=True, lock_dir=lock_dir
    )
    assert len(calls) == 1  # called + raised, but loop survived
    assert not os.path.exists(lock_dir)  # lock released on the finally path


async def test_run_supervised_isolates_a_crashing_cotask(monkeypatch) -> None:
    """_run_supervised: a crashing factory is logged + restarted after
    backoff and NEVER propagates (one co-task crash must not kill its
    sibling or the daemon). CancelledError propagates (clean shutdown)."""
    stop_event = asyncio.Event()
    attempts = {"n": 0}

    async def crashing_factory():
        attempts["n"] += 1
        raise RuntimeError("co-task boom")

    # Patch the backoff sleep to immediately stop after the first crash
    # so the test is deterministic + fast.
    async def stop_after_first(coro, timeout):  # noqa: ANN001
        stop_event.set()
        coro.close()
        return None

    monkeypatch.setattr(lts.asyncio, "wait_for", stop_after_first)
    # Must return cleanly (no propagation) despite the factory raising.
    await lts._run_supervised("engine", crashing_factory, stop_event)  # noqa: SLF001
    assert attempts["n"] >= 1  # it ran (and crashed) at least once


async def test_run_supervised_cancellederror_propagates() -> None:
    """CancelledError must propagate out of _run_supervised (clean
    shutdown path) — it is NOT swallowed like a normal Exception."""
    stop_event = asyncio.Event()

    async def cancel_factory():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await lts._run_supervised("engine", cancel_factory, stop_event)  # noqa: SLF001


async def test_both_lanes_share_the_one_pool_and_lock(
    monkeypatch, lock_dir
) -> None:
    """The data + engine co-tasks both fire on the SAME pool object and
    serialize on the SAME mkdir-atomic self-exclusion lock (so an engine
    pass and a data pass can never race `git worktree add`)."""
    recent = datetime.now(UTC) - timedelta(minutes=1)
    pool = _Pool(
        events=[
            (recent, "DATA_REPAIR_ESCALATED"),
            (recent, "ENGINE_ESCALATED"),
        ]
    )
    data_calls = await _run_one_loop(monkeypatch, pool, lock_dir=lock_dir)
    eng_calls = await _run_one_engine_loop(
        monkeypatch, pool, lock_dir=lock_dir
    )
    assert data_calls == [pool]  # data lane: shared pool
    assert eng_calls == [pool]  # engine lane: SAME shared pool
    assert not os.path.exists(lock_dir)  # SAME lock, released by both


# ════════════════════════════════════════════════════════════════════════
# (vii) Concurrent cross-lane crash-isolation (integration-style).
#
# Both co-tasks run under ONE asyncio.gather (mirrors _amain's gather
# shape). The ENGINE factory raises on every invocation — a factory-level
# crash that reaches _run_supervised directly (the scenario where
# _run_supervised is the ONLY guard between a crashing lane and the
# gather). The DATA factory drives a genuine poll via the real
# _lane_loop. Assert: (a) data lane completes its triage pass; (b) the
# gather returns without raising; (c) no exception escapes.
#
# Biting guarantee: if _run_supervised were changed to re-raise instead
# of logging+restarting, the engine task exception propagates into
# asyncio.gather, which cancels the data task → data_calls stays empty
# → assertion (a) FAILS. Proved below by temporarily patching
# _run_supervised to re-raise and confirming the test fails.
# ════════════════════════════════════════════════════════════════════════


async def test_concurrent_engine_crash_does_not_kill_data_lane(
    monkeypatch, tmp_path
) -> None:
    """Run both _run_supervised co-tasks concurrently under asyncio.gather.
    The engine factory raises on every call (factory-level crash). Assert
    the data lane still processes its trigger, the gather does not crash,
    and no unhandled exception escapes."""

    data_lock = os.path.join(str(tmp_path), "data.lock")

    recent = datetime.now(UTC) - timedelta(minutes=1)
    pool = _Pool(events=[(recent, "DATA_REPAIR_ESCALATED")])

    data_calls: list = []

    async def data_recovery(p):
        data_calls.append(p)
        return None

    monkeypatch.setattr(lts, "run_autonomous_recovery", data_recovery)
    # Startup prune is covered by its own test; skip the git call here.
    monkeypatch.setattr(lts, "_startup_worktree_prune", lambda: None)

    stop_event = asyncio.Event()
    real_wait_for = asyncio.wait_for

    async def stop_after_data_poll(coro, timeout):  # noqa: ANN001
        # Yield so the engine task can be scheduled before we stop.
        await asyncio.sleep(0)
        stop_event.set()
        coro.close()
        return None

    monkeypatch.setattr(lts.asyncio, "wait_for", stop_after_data_poll)

    async def _data_factory():
        await lts._main_loop(pool, stop_event, data_lock)  # noqa: SLF001

    # Engine factory raises directly — a factory-level crash that reaches
    # _run_supervised before _lane_loop can absorb it.
    engine_factory_calls = {"n": 0}

    async def _crashing_engine_factory():
        engine_factory_calls["n"] += 1
        raise RuntimeError("engine factory boom — _run_supervised must isolate this")

    try:
        # Must NOT raise — the engine crash must stay inside _run_supervised.
        await asyncio.gather(
            lts._run_supervised("data", _data_factory, stop_event),  # noqa: SLF001
            lts._run_supervised("engine", _crashing_engine_factory, stop_event),  # noqa: SLF001
        )
    finally:
        monkeypatch.setattr(lts.asyncio, "wait_for", real_wait_for)

    # (a) Data lane processed its trigger normally.
    assert len(data_calls) >= 1, (
        "data lane run_triage was never called — engine crash leaked into gather"
    )
    assert data_calls[0] is pool

    # (b)+(c) Gather returned without raising → proven by reaching this line.
    # Also confirm the engine factory WAS called (not a no-op).
    assert engine_factory_calls["n"] >= 1, (
        "engine factory was never called — test didn't exercise the crash path"
    )


def test_two_daemon_invariant_still_passes_unedited() -> None:
    """B1 placement proof: the topology invariant — the closed 4-token
    installer whitelist + the launchd label set — must be byte-unchanged.
    Header-comment edits to ``install_all_daemons.sh`` are allowed (the
    2026-05-21 autonomous-data-lane flip updates the operator-facing
    docstring for the data lane; the topology / whitelist / labels are
    NOT touched). The bite is preserved by running
    ``test_two_daemon_invariant.py`` itself — that test pins the
    ``for installer in …; do`` token set + the retired-installer set +
    the dashboard daemon spec, and would still red on a topology
    regression."""
    import subprocess as _sp

    repo = Path(__file__).resolve().parent.parent
    # The topology test file MUST be byte-unchanged (no in-test relaxation
    # of the whitelist) — that is the structural bite.
    diff = _sp.run(
        ["git", "diff", "--stat", "HEAD", "--",
         "scripts/tests/test_two_daemon_invariant.py"],
        cwd=str(repo), capture_output=True, text=True, check=False,
    )
    assert diff.stdout.strip() == "", (
        "B1 violation: the topology test was edited — "
        f"placement is wrong:\n{diff.stdout}"
    )
    r = _sp.run(
        [sys.executable, "-m", "pytest", "-q",
         "scripts/tests/test_two_daemon_invariant.py"],
        cwd=str(repo), capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, (
        f"test_two_daemon_invariant.py FAILED:\n{r.stdout}\n{r.stderr}"
    )
