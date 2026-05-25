"""Tests for Wave-4 E4: ``tpcore.aar.deferred`` + the ``AARWriter`` self-heal seam.

The shape mirrors ``tpcore/tests/test_aar_writer.py`` — a small fake
asyncpg pool that records calls so we can assert SQL + payload shape
without spinning up a real Postgres. The spec contract under test:

* On a successful ``aar_events`` INSERT, no defer happens.
* On ``aar_events`` INSERT raise, ``write_aar`` returns ``False`` (does
  NOT re-raise), the AAR is enqueued via :class:`DeferredAARWriter`,
  and an ``AAR_DEFERRED`` event is emitted to ``platform.application_log``.
* :func:`replay_deferred_aars` drains pending rows, marks
  ``replayed_at`` on success, leaves failures pending for retry.
* The ``self_heal=False`` escape hatch retains the pre-Wave-4 raise
  behavior (used by the replay path itself to avoid infinite-loop
  re-deferral).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tpcore.aar.deferred import (
    AAR_DEFERRED_EVENT,
    DeferredAARWriter,
    replay_deferred_aars,
)
from tpcore.aar.models import AfterActionReport, ExitReason
from tpcore.aar.writer import AARWriter

# ────────────────────────────────────────────────────────────────────────
# Fake asyncpg pool — kept local on purpose (mirrors test_aar_writer.py)
# ────────────────────────────────────────────────────────────────────────


class _FakeConn:
    """Records SQL calls; ``fetchrow``/``fetch``/``execute`` are
    programmable via the parent pool's ``script`` queue."""

    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool
        self.calls: list[tuple[str, str, tuple]] = []

    async def fetchrow(self, sql: str, *args):
        self.calls.append(("fetchrow", sql, args))
        return self._pool.next_result("fetchrow", sql, args)

    async def fetch(self, sql: str, *args):
        self.calls.append(("fetch", sql, args))
        return self._pool.next_result("fetch", sql, args)

    async def execute(self, sql: str, *args):
        self.calls.append(("execute", sql, args))
        return self._pool.next_result("execute", sql, args)

    async def fetchval(self, sql: str, *args):
        # PR-12: AARWriter uses IdentityDispatcher (fetchval against
        # ticker_history) to resolve classification_id at write time.
        # Default behavior — return None ⇒ aar_events.classification_id
        # column stays NULL (acceptable; column is nullable). Tests that
        # want to assert on cid plumbing can queue via the pool.
        self.calls.append(("fetchval", sql, args))
        return self._pool.next_result("fetchval", sql, args)


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    """A scriptable fake. Each call type pops the next value from its
    queue (FIFO). Falls back to ``None`` when the queue empties — most
    tests only care about the early calls.
    """

    def __init__(self) -> None:
        self.conn = _FakeConn(self)
        self._fetchrow_queue: list = []
        self._fetch_queue: list = []
        self._execute_queue: list = []
        # If set, every method raises this exception on every call.
        self.raise_on_acquire: BaseException | None = None
        # If set, only ``fetchrow`` raises (used for the AAR-write-fail
        # injection point).
        self.raise_on_fetchrow: BaseException | None = None

    def queue_fetchrow(self, value) -> None:
        self._fetchrow_queue.append(value)

    def queue_fetch(self, value) -> None:
        self._fetch_queue.append(value)

    def queue_execute(self, value) -> None:
        self._execute_queue.append(value)

    def next_result(self, method: str, sql: str, args: tuple):
        if self.raise_on_fetchrow is not None and method == "fetchrow":
            # First call eats the injection; clear so the defer path's
            # fetchrow returns the deferred-row id normally.
            exc = self.raise_on_fetchrow
            if "INSERT INTO platform.aar_events" in sql:
                self.raise_on_fetchrow = None
                raise exc
        if method == "fetchrow":
            return self._fetchrow_queue.pop(0) if self._fetchrow_queue else None
        if method == "fetch":
            return self._fetch_queue.pop(0) if self._fetch_queue else []
        if method == "fetchval":
            # PR-12: dispatcher ticker→cid lookup. Default None (column
            # stays NULL). The tests that exercise the defer path don't
            # care about cid resolution.
            return None
        return self._execute_queue.pop(0) if self._execute_queue else None

    def acquire(self) -> _FakeAcquireCM:
        if self.raise_on_acquire is not None:
            raise self.raise_on_acquire
        return _FakeAcquireCM(self.conn)


def _aar(trade_id: str = "rev-AAPL-001") -> AfterActionReport:
    return AfterActionReport(
        engine="reversion",
        trade_id=trade_id,
        ticker="AAPL",
        entry_ts=datetime(2026, 5, 9, 13, 30, tzinfo=UTC),
        exit_ts=datetime(2026, 5, 9, 19, 55, tzinfo=UTC),
        entry_price=Decimal("180.00"),
        exit_price=Decimal("184.00"),
        qty=Decimal("4"),
        confidence_at_entry=Decimal("0.80"),
        sizing_pct_of_engine_equity=Decimal("0.144"),
        pnl_gross=Decimal("16.00"),
        pnl_net=Decimal("16.00"),
        exit_reason=ExitReason.TIER1_MID_BAND,
        rule_compliance=True,
    )


def _app_log_inserts(conn: _FakeConn) -> list[tuple]:
    """Return only the application_log INSERT calls."""
    return [
        (method, sql, args)
        for (method, sql, args) in conn.calls
        if "INSERT INTO platform.application_log" in sql
    ]


# ────────────────────────────────────────────────────────────────────────
# DeferredAARWriter
# ────────────────────────────────────────────────────────────────────────


async def test_defer_no_pool_returns_none() -> None:
    """No pool → defer is a structlog warning + None (matches the
    pre-existing ``AARWriter`` no-pool contract)."""
    writer = DeferredAARWriter(db_pool=None)
    result = await writer.defer(_aar(), RuntimeError("substrate down"))
    assert result is None


async def test_defer_inserts_row_and_emits_event() -> None:
    """Defer happy path: INSERT INTO aar_deferred + emit AAR_DEFERRED."""
    pool = _FakePool()
    # First fetchrow = the deferred-row INSERT … RETURNING id.
    pool.queue_fetchrow({"id": "deadbeef-id"})
    writer = DeferredAARWriter(pool)
    aar = _aar()
    result = await writer.defer(aar, RuntimeError("pool exhausted"))

    assert result == "deadbeef-id"
    # First call was the deferred INSERT.
    method, sql, args = pool.conn.calls[0]
    assert method == "fetchrow"
    assert "INSERT INTO platform.aar_deferred" in sql
    # engine, trade_id, ticker, aar_data JSON, defer_reason
    assert args[0] == "reversion"
    assert args[1] == "rev-AAPL-001"
    assert args[2] == "AAPL"
    assert "RuntimeError: pool exhausted" in args[4]
    # Followed by the AAR_DEFERRED application_log INSERT.
    inserts = _app_log_inserts(pool.conn)
    assert len(inserts) == 1
    _, sql, app_args = inserts[0]
    assert app_args[0] == "reversion"  # engine
    assert app_args[2] == AAR_DEFERRED_EVENT
    assert app_args[3] == "WARNING"
    payload = json.loads(app_args[5])
    assert payload["engine"] == "reversion"
    assert payload["trade_id"] == "rev-AAPL-001"
    assert payload["ticker"] == "AAPL"


async def test_defer_truncates_long_exception_message() -> None:
    """A 10_000-char exception message must not bloat the defer_reason column."""
    pool = _FakePool()
    pool.queue_fetchrow({"id": "x"})
    writer = DeferredAARWriter(pool)
    long_msg = "x" * 10_000
    await writer.defer(_aar(), RuntimeError(long_msg))
    _, _, args = pool.conn.calls[0]
    defer_reason = args[4]
    # Cap is 480; class name "RuntimeError: " is 14 chars + ellipsis 3 chars.
    assert len(defer_reason) <= 480
    assert defer_reason.startswith("RuntimeError: ")
    assert defer_reason.endswith("...")


async def test_defer_swallows_insert_failure() -> None:
    """If the defer-INSERT itself raises, return None — the engine
    cycle must continue even when the defer substrate is also down."""
    pool = _FakePool()
    pool.raise_on_acquire = RuntimeError("totally offline")
    writer = DeferredAARWriter(pool)
    result = await writer.defer(_aar(), RuntimeError("orig"))
    assert result is None


# ────────────────────────────────────────────────────────────────────────
# AARWriter integration — write_aar exception path defers
# ────────────────────────────────────────────────────────────────────────


async def test_write_aar_on_exception_defers_and_returns_false() -> None:
    """The full self-heal seam: AAR-events INSERT raises → DeferredAARWriter
    queues + emits AAR_DEFERRED → write_aar returns False (no re-raise)."""
    pool = _FakePool()
    # AAR-events INSERT raises.
    pool.raise_on_fetchrow = RuntimeError("aar_events insert exploded")
    # The defer path's INSERT INTO aar_deferred fetchrow returns an id.
    pool.queue_fetchrow({"id": "defer-id-1"})
    writer = AARWriter(pool)
    wrote = await writer.write_aar(_aar())
    assert wrote is False
    # Three SQL calls: the failed aar_events fetchrow, the
    # aar_deferred fetchrow, the application_log execute.
    methods_sqls = [(m, sql) for (m, sql, _) in pool.conn.calls]
    assert any(m == "fetchrow" and "INSERT INTO platform.aar_events" in sql for (m, sql) in methods_sqls)
    assert any(m == "fetchrow" and "INSERT INTO platform.aar_deferred" in sql for (m, sql) in methods_sqls)
    assert any(m == "execute" and "INSERT INTO platform.application_log" in sql for (m, sql) in methods_sqls)


async def test_write_aar_self_heal_false_reraises() -> None:
    """``self_heal=False`` retains pre-Wave-4 contract — used by the
    replay path so it doesn't infinitely re-defer rows whose underlying
    write still fails."""
    pool = _FakePool()
    pool.raise_on_fetchrow = RuntimeError("still down")
    writer = AARWriter(pool, self_heal=False)
    with pytest.raises(RuntimeError, match="still down"):
        await writer.write_aar(_aar())


# ────────────────────────────────────────────────────────────────────────
# replay_deferred_aars
# ────────────────────────────────────────────────────────────────────────


async def test_replay_no_pool_returns_zero_counts() -> None:
    counts = await replay_deferred_aars(None, limit=10)
    assert counts == {"pending": 0, "replayed": 0, "still_failing": 0}


async def test_replay_drains_pending_rows() -> None:
    """Pending row → write_aar succeeds → mark replayed_at → counts.replayed=1."""
    pool = _FakePool()
    aar = _aar(trade_id="rev-AAPL-002")
    aar_data_json = aar.model_dump_json()
    # _SELECT_PENDING_SQL pulls fetch().
    pool.queue_fetch(
        [
            {
                "id": "defer-row-1",
                "engine": aar.engine,
                "trade_id": aar.trade_id,
                "ticker": aar.ticker,
                "aar_data": aar_data_json,
                "defer_reason": "RuntimeError: x",
                "recorded_at": datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
            }
        ]
    )
    # write_aar's fetchrow for aar_events INSERT — RETURNING 1 fires.
    pool.queue_fetchrow({"?column?": 1})
    # _MARK_REPLAYED_SQL fires execute().
    pool.queue_execute(None)
    counts = await replay_deferred_aars(pool, limit=10)
    assert counts["pending"] == 1
    assert counts["replayed"] == 1
    assert counts["still_failing"] == 0


async def test_replay_failing_write_keeps_row_pending() -> None:
    """A row whose ``aar_events`` insert STILL raises stays in the
    queue (no replayed_at mark)."""
    pool = _FakePool()
    aar = _aar(trade_id="rev-AAPL-003")
    pool.queue_fetch(
        [
            {
                "id": "defer-row-2",
                "engine": aar.engine,
                "trade_id": aar.trade_id,
                "ticker": aar.ticker,
                "aar_data": aar.model_dump_json(),
                "defer_reason": "RuntimeError: x",
                "recorded_at": datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
            }
        ]
    )
    pool.raise_on_fetchrow = RuntimeError("still failing")
    counts = await replay_deferred_aars(pool, limit=10)
    assert counts["pending"] == 1
    assert counts["replayed"] == 0
    assert counts["still_failing"] == 1


async def test_replay_handles_corrupted_aar_data_row() -> None:
    """A row with un-rehydrateable JSON is counted as still_failing
    (logged + skipped; the replay never crashes the loop)."""
    pool = _FakePool()
    pool.queue_fetch(
        [
            {
                "id": "defer-corrupt",
                "engine": "reversion",
                "trade_id": "rev-X",
                "ticker": "X",
                "aar_data": "not-valid-json",
                "defer_reason": "RuntimeError: x",
                "recorded_at": datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
            }
        ]
    )
    counts = await replay_deferred_aars(pool, limit=10)
    assert counts["pending"] == 1
    assert counts["still_failing"] == 1
    assert counts["replayed"] == 0
