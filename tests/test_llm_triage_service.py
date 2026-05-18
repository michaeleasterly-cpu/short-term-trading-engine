"""Unit tests for ops/llm_triage_service.py — the event-driven LLM-triage
daemon (LT-P3 4d).

Structural sibling of tests/test_data_repair_service.py: a fake asyncpg
pool (no DB), ``run_triage`` monkeypatched (no LLM, no real triage), and
the cursor-advance behaviour asserted directly via ``_main_loop`` /
``_find_new_trigger``.

Coverage:
  (i)   no trigger event           → run_triage NOT called, cursor unchanged
  (ii)  DATA_REPAIR_ESCALATED newer→ run_triage called once, cursor advances
  (iii) run_triage raising         → daemon loop survives (no crash), logged
  (iv)  DATA_SOURCE_ESCALATED      → also triggers
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
# ops.llm_data_triage import run_triage`` at module top-level. That
# intra-package import fails ("'ops' is not a package") whenever the
# scripts single-file ``ops`` is shadowing sys.modules. We must NOT
# mutate the shared sys.modules['ops'] permanently (that breaks
# test_ops_helpers when this module collects between it and
# scripts/tests). Instead: snapshot the relevant sys.modules entries,
# install a minimal collision-free stub for ``ops.llm_data_triage``
# (every test here monkeypatches ``lts.run_triage`` so the real impl is
# NEVER exercised), exec the daemon by file path, then RESTORE
# sys.modules exactly — zero global side effects, collection-order safe.
_LTS_PATH = Path(__file__).resolve().parent.parent / "ops" / "llm_triage_service.py"
_SAVED = {k: sys.modules.get(k) for k in ("ops", "ops.llm_data_triage")}
try:
    _ops = sys.modules.get("ops")
    if not isinstance(getattr(_ops, "__path__", None), list):
        _pkg = types.ModuleType("ops")
        _pkg.__path__ = [str(_LTS_PATH.parent)]  # make it package-shaped
        sys.modules["ops"] = _pkg
    _stub = types.ModuleType("ops.llm_data_triage")

    async def _stub_run_triage(*_a, **_k):  # pragma: no cover - replaced
        raise AssertionError("run_triage must be monkeypatched in this test")

    _stub.run_triage = _stub_run_triage
    sys.modules["ops.llm_data_triage"] = _stub

    _spec = importlib.util.spec_from_file_location("_lts_under_test", _LTS_PATH)
    assert _spec is not None and _spec.loader is not None
    lts = importlib.util.module_from_spec(_spec)
    sys.modules["_lts_under_test"] = lts
    _spec.loader.exec_module(lts)
finally:
    # Restore sys.modules['ops'] / ['ops.llm_data_triage'] EXACTLY so no
    # later-collected test (e.g. test_ops_helpers) sees our scaffolding.
    for _k, _v in _SAVED.items():
        if _v is None:
            sys.modules.pop(_k, None)
        else:
            sys.modules[_k] = _v


# ────────────────────────────────────────────────────────────────────────
# Fakes (mirror tests/test_data_repair_service.py).
# ────────────────────────────────────────────────────────────────────────


class _Conn:
    def __init__(self, pool: _Pool) -> None:
        self._pool = pool

    async def fetchrow(self, sql: str, *args):
        # _find_new_trigger: newest trigger with recorded_at > cursor.
        if "ORDER BY recorded_at DESC" in sql:
            cursor = args[1]
            hits = [
                ts
                for ts, et in self._pool.events
                if et in lts.TRIGGER_EVENT_TYPES and ts > cursor
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
    the list of run_triage call markers."""
    calls: list = []

    async def fake_run_triage(p):
        calls.append(p)
        if raises:
            raise RuntimeError("triage boom")
        return None

    monkeypatch.setattr(lts, "run_triage", fake_run_triage)

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
        await lts._main_loop(pool, stop_event, ld)
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
    newest = await lts._find_new_trigger(pool, cursor)
    assert newest == recent  # the daemon advances its cursor to this
    # After advancing, the same event no longer re-triggers.
    assert await lts._find_new_trigger(pool, recent) is None


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


def test_trigger_event_types_are_the_two_escalations() -> None:
    # Structural: the daemon triggers on data-lane escalations only.
    assert lts.TRIGGER_EVENT_TYPES == (
        "DATA_REPAIR_ESCALATED",
        "DATA_SOURCE_ESCALATED",
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
    assert str(prune_calls[0][1]["cwd"]) == str(lts._REPO_ROOT)


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
    lts._startup_worktree_prune()  # must NOT raise
