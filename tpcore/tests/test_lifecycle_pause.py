"""Tests for Wave-4 E7 + E11: ``tpcore.risk.lifecycle_pause``.

Contract under test:

* :func:`check_credibility_drop` reads N=3 (default) recent rows for
  source ``backtest_credibility.<engine>`` from
  ``platform.data_quality_log``. If every row's ``confidence``
  is below ``MIN_LIVE_SCORE/100`` AND no existing hold, emits
  ``ENGINE_CREDIBILITY_DROP`` + ``ENGINE_HELD`` (failure_class
  ``behavioral_credibility``).
* :func:`check_lifecycle_degraded` mirrors the above with N=5 default,
  source ``engine_lifecycle.<engine>``, event
  ``ENGINE_LIFECYCLE_DEGRADED``.
* One-hold rule: when ``current_hold`` returns a HoldState, the
  check is a no-op (no new emit).
* Partial-degradation (one above-floor row in the window) is a no-op.
* Insufficient history (fewer than N rows) is a no-op.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from tpcore.risk.lifecycle_pause import (
    ENGINE_CREDIBILITY_DROP_EVENT,
    ENGINE_LIFECYCLE_DEGRADED_EVENT,
    check_credibility_drop,
    check_lifecycle_degraded,
)
from tpcore.supervisor_state import HELD_EVENT


class _FakeConn:
    """Programmable fake — caller queues fetch responses; execute calls
    are recorded so we can assert the emit payloads."""

    def __init__(self) -> None:
        self.fetch_queue: list = []
        self.fetchrow_queue: list = []
        self.executes: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        if self.fetch_queue:
            return self.fetch_queue.pop(0)
        return []

    async def fetchrow(self, sql: str, *args):
        if self.fetchrow_queue:
            return self.fetchrow_queue.pop(0)
        return None

    async def execute(self, sql: str, *args):
        self.executes.append((sql, args))
        return None


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(self.conn)


def _app_log_inserts(conn: _FakeConn) -> list[tuple]:
    return [
        (sql, args)
        for (sql, args) in conn.executes
        if "INSERT INTO platform.application_log" in sql
    ]


def _confidence_row(confidence: float, ts: datetime) -> dict:
    """Mirror the asyncpg Record shape from ``platform.data_quality_log``."""
    return {"confidence": Decimal(str(confidence)), "timestamp": ts}


# ────────────────────────────────────────────────────────────────────────
# check_credibility_drop (E7)
# ────────────────────────────────────────────────────────────────────────


async def test_credibility_drop_no_pool_returns_false() -> None:
    assert await check_credibility_drop(None, engine="reversion") is False


async def test_credibility_drop_three_sub_floor_rows_pauses() -> None:
    pool = _FakePool()
    # current_hold's fetchrow returns None ⇒ no existing hold.
    pool.conn.fetchrow_queue.append(None)
    # _read_recent_confidences fetch: three sub-floor scores (0.45 < 0.60).
    pool.conn.fetch_queue.append(
        [
            _confidence_row(0.45, datetime(2026, 5, 22, 12, 0, tzinfo=UTC)),
            _confidence_row(0.50, datetime(2026, 5, 21, 12, 0, tzinfo=UTC)),
            _confidence_row(0.55, datetime(2026, 5, 20, 12, 0, tzinfo=UTC)),
        ]
    )
    paused = await check_credibility_drop(pool, engine="reversion")
    assert paused is True
    inserts = _app_log_inserts(pool.conn)
    # One ENGINE_CREDIBILITY_DROP + one ENGINE_HELD.
    assert len(inserts) == 2
    events = [args[2] for (_, args) in inserts]
    assert ENGINE_CREDIBILITY_DROP_EVENT in events
    assert HELD_EVENT in events
    # Payload on ENGINE_HELD has behavioral_credibility failure_class.
    held = next(args for (_, args) in inserts if args[2] == HELD_EVENT)
    payload = json.loads(held[5])
    assert payload["failure_class"] == "behavioral_credibility"
    # ENGINE_CREDIBILITY_DROP payload has the source + recent_confidences.
    drop = next(
        args for (_, args) in inserts if args[2] == ENGINE_CREDIBILITY_DROP_EVENT
    )
    drop_payload = json.loads(drop[5])
    assert drop_payload["source"] == "backtest_credibility.reversion"
    assert drop_payload["recent_confidences"] == [0.45, 0.50, 0.55]
    assert drop_payload["floor_score"] == 60


async def test_credibility_drop_one_above_floor_is_noop() -> None:
    pool = _FakePool()
    pool.conn.fetchrow_queue.append(None)
    pool.conn.fetch_queue.append(
        [
            _confidence_row(0.45, datetime(2026, 5, 22, 12, 0, tzinfo=UTC)),
            _confidence_row(0.70, datetime(2026, 5, 21, 12, 0, tzinfo=UTC)),  # above floor
            _confidence_row(0.55, datetime(2026, 5, 20, 12, 0, tzinfo=UTC)),
        ]
    )
    paused = await check_credibility_drop(pool, engine="reversion")
    assert paused is False
    assert _app_log_inserts(pool.conn) == []


async def test_credibility_drop_insufficient_history_is_noop() -> None:
    pool = _FakePool()
    pool.conn.fetchrow_queue.append(None)
    # Only TWO sub-floor rows — N=3 default → not enough history.
    pool.conn.fetch_queue.append(
        [
            _confidence_row(0.45, datetime(2026, 5, 22, 12, 0, tzinfo=UTC)),
            _confidence_row(0.50, datetime(2026, 5, 21, 12, 0, tzinfo=UTC)),
        ]
    )
    paused = await check_credibility_drop(pool, engine="reversion")
    assert paused is False


async def test_credibility_drop_existing_hold_skips() -> None:
    """One-hold rule — existing HoldState from current_hold blocks emit."""
    pool = _FakePool()
    # current_hold's fetchrow returns a non-None hold row.
    pool.conn.fetchrow_queue.append(
        {
            "hold_id": "existing-hold",
            "failure_class": "crashed_startup",
            "reason": "previous",
            "held_at": datetime(2026, 5, 22, 11, 0, tzinfo=UTC),
            "cleared": None,  # uncleared
        }
    )
    paused = await check_credibility_drop(pool, engine="reversion")
    assert paused is False
    # Critically: fetch was NEVER called — we short-circuited.
    assert _app_log_inserts(pool.conn) == []


# ────────────────────────────────────────────────────────────────────────
# check_lifecycle_degraded (E11)
# ────────────────────────────────────────────────────────────────────────


async def test_lifecycle_degraded_five_sub_floor_rows_pauses() -> None:
    pool = _FakePool()
    pool.conn.fetchrow_queue.append(None)  # no existing hold
    # N=5 default — five sub-floor rows.
    pool.conn.fetch_queue.append(
        [
            _confidence_row(0.40, datetime(2026, 5, 22 - i, 12, 0, tzinfo=UTC))
            for i in range(5)
        ]
    )
    paused = await check_lifecycle_degraded(pool, engine="vector")
    assert paused is True
    inserts = _app_log_inserts(pool.conn)
    events = [args[2] for (_, args) in inserts]
    assert ENGINE_LIFECYCLE_DEGRADED_EVENT in events
    assert HELD_EVENT in events
    held = next(args for (_, args) in inserts if args[2] == HELD_EVENT)
    payload = json.loads(held[5])
    assert payload["failure_class"] == "behavioral_lifecycle"


async def test_lifecycle_degraded_existing_hold_skips() -> None:
    pool = _FakePool()
    pool.conn.fetchrow_queue.append(
        {
            "hold_id": "existing",
            "failure_class": "behavioral",
            "reason": "previous",
            "held_at": datetime(2026, 5, 22, 11, 0, tzinfo=UTC),
            "cleared": None,
        }
    )
    paused = await check_lifecycle_degraded(pool, engine="vector")
    assert paused is False


async def test_lifecycle_degraded_explicit_threshold_overrides_default() -> None:
    """``threshold`` kwarg lets the operator/test bypass the env default."""
    pool = _FakePool()
    pool.conn.fetchrow_queue.append(None)
    pool.conn.fetch_queue.append(
        [
            _confidence_row(0.40, datetime(2026, 5, 22, 12, 0, tzinfo=UTC)),
            _confidence_row(0.40, datetime(2026, 5, 21, 12, 0, tzinfo=UTC)),
        ]
    )
    # Lower the threshold to 2 → 2 sub-floor rows trip the pause.
    paused = await check_lifecycle_degraded(
        pool, engine="vector", threshold=2,
    )
    assert paused is True


async def test_lifecycle_degraded_custom_floor_pct() -> None:
    """A 0.80 floor with 0.75 confidences trips even though 0.75 > 0.60 default."""
    pool = _FakePool()
    pool.conn.fetchrow_queue.append(None)
    pool.conn.fetch_queue.append(
        [
            _confidence_row(0.75, datetime(2026, 5, 22 - i, 12, 0, tzinfo=UTC))
            for i in range(5)
        ]
    )
    paused = await check_lifecycle_degraded(
        pool, engine="vector", floor_pct=0.80,
    )
    assert paused is True
