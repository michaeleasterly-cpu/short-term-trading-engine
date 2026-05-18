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
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# NOTE: ``scripts/ops.py`` is a single-file module that some other test
# imports as the top-level name ``ops`` after putting ``scripts/`` on
# sys.path — that permanently shadows the ``ops/`` *package* in
# sys.modules for the whole pytest session. So ``import
# ops.llm_triage_service`` is collection-order fragile. Load the module
# by file path under a private, collision-free name instead — robust
# regardless of which test ran first (mirrors test_data_repair_service).
_LTS_PATH = Path(__file__).resolve().parents[2] / "ops" / "llm_triage_service.py"
_spec = importlib.util.spec_from_file_location("_lts_under_test", _LTS_PATH)
assert _spec is not None and _spec.loader is not None
lts = importlib.util.module_from_spec(_spec)
sys.modules["_lts_under_test"] = lts
_spec.loader.exec_module(lts)


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


async def _run_one_loop(monkeypatch, pool, *, raises: bool = False) -> list:
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
    try:
        await lts._main_loop(pool, stop_event)
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


async def test_repair_escalated_triggers_run_triage_once(monkeypatch) -> None:
    recent = datetime.now(UTC) - timedelta(minutes=1)
    pool = _Pool(events=[(recent, "DATA_REPAIR_ESCALATED")])
    calls = await _run_one_loop(monkeypatch, pool)
    assert len(calls) == 1  # fired exactly once
    assert calls[0] is pool  # run_triage(pool) — the shared pool


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


async def test_run_triage_exception_does_not_crash_loop(monkeypatch) -> None:
    recent = datetime.now(UTC) - timedelta(minutes=1)
    pool = _Pool(events=[(recent, "DATA_SOURCE_ESCALATED")])
    # raises=True → fake_run_triage raises RuntimeError. The loop must
    # complete normally (no propagation) — crash-isolated, advisory.
    calls = await _run_one_loop(monkeypatch, pool, raises=True)
    assert len(calls) == 1  # it WAS called (and raised) — but loop survived


# ────────────────────────────────────────────────────────────────────────
# (iv) DATA_SOURCE_ESCALATED also triggers.
# ────────────────────────────────────────────────────────────────────────


async def test_data_source_escalated_also_triggers(monkeypatch) -> None:
    recent = datetime.now(UTC) - timedelta(minutes=1)
    pool = _Pool(events=[(recent, "DATA_SOURCE_ESCALATED")])
    calls = await _run_one_loop(monkeypatch, pool)
    assert len(calls) == 1  # DATA_SOURCE_ESCALATED is a trigger too


def test_trigger_event_types_are_the_two_escalations() -> None:
    # Structural: the daemon triggers on data-lane escalations only.
    assert lts.TRIGGER_EVENT_TYPES == (
        "DATA_REPAIR_ESCALATED",
        "DATA_SOURCE_ESCALATED",
    )
