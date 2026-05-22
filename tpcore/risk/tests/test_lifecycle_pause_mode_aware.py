"""Sentinel — Wave-4 E7 + E11 mode-aware credibility floor.

PR ``feat/lifecycle-pause-mode-aware-credibility-floor`` makes the
E7/E11 lifecycle-pause floor read the engine's
``EngineProfile.lifecycle_state`` and apply the correct floor per state:

* ``LifecycleState.PAPER`` → ``MIN_PAPER_SCORE`` (0.30 default).
* ``LifecycleState.LIVE`` → ``MIN_LIVE_SCORE`` (0.60 default).
* ``LifecycleState.RETIRED`` — engine never dispatched (excluded from
  ``_DISPATCHABLE`` in :mod:`tpcore.engine_profile`); skipped here.

Operator directive 2026-05-22: paper engines must NOT be paused by the
live-promotion floor because the autonomous-Lab admit pathway (PR #158)
lands engines at credibility ~0.40-0.50; immediate E11-pause defeats
paper trade-history accumulation.

This is the regression sentinel — its scenarios cover the four headline
table rows from the PR brief. The four currently-paused PAPER engines
(reversion, vector, sentinel, momentum) are all in the 0.40-0.55 range,
above the paper floor 0.30, and so under the mode-aware logic NONE of
them should trip the pause.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

from tpcore.engine_profile import LifecycleState
from tpcore.risk.lifecycle_pause import (
    ENGINE_CREDIBILITY_DROP_EVENT,
    ENGINE_LIFECYCLE_DEGRADED_EVENT,
    check_credibility_drop,
    check_lifecycle_degraded,
)
from tpcore.supervisor_state import HELD_EVENT

# ────────────────────────────────────────────────────────────────────────
# Fakes — mirror the asyncpg.Pool / Record shape the production code uses
# ────────────────────────────────────────────────────────────────────────


class _FakeConn:
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


def _confidence_row(confidence: float, ts: datetime) -> dict:
    return {"confidence": Decimal(str(confidence)), "timestamp": ts}


def _app_log_inserts(conn: _FakeConn) -> list[tuple]:
    return [
        (sql, args)
        for (sql, args) in conn.executes
        if "INSERT INTO platform.application_log" in sql
    ]


def _stub_profile(engine: str, state: LifecycleState):
    """Patch ``profile_for`` inside ``tpcore.risk.lifecycle_pause`` to
    return a profile with the requested lifecycle state. Engine name
    is preserved so the source key + reason text are realistic.

    Uses a lightweight stand-in object (not the real ``EngineProfile``
    constructor) so the test stays decoupled from the SoT's required
    fields — ``_credibility_floor_pct_for`` / ``_lifecycle_floor_pct_for``
    only read ``.lifecycle_state``.
    """

    class _Stub:
        lifecycle_state = state

    return patch(
        "tpcore.risk.lifecycle_pause.profile_for",
        return_value=_Stub(),
    )


# ────────────────────────────────────────────────────────────────────────
# E7 — check_credibility_drop, PAPER mode-aware floor (0.30)
# ────────────────────────────────────────────────────────────────────────


async def test_e7_paper_engine_credibility_0_45_no_pause() -> None:
    """PAPER + credibility 0.45 (3 consecutive) → NO pause.

    0.45 is above the paper floor (0.30) so the autonomous-Lab admit
    pathway is preserved.
    """
    pool = _FakePool()
    pool.conn.fetchrow_queue.append(None)  # no existing hold
    pool.conn.fetch_queue.append(
        [
            _confidence_row(0.45, datetime(2026, 5, 22 - i, 12, 0, tzinfo=UTC))
            for i in range(3)
        ]
    )
    with _stub_profile("reversion", LifecycleState.PAPER):
        paused = await check_credibility_drop(pool, engine="reversion")
    assert paused is False
    assert _app_log_inserts(pool.conn) == []


async def test_e7_paper_engine_credibility_0_20_pauses() -> None:
    """PAPER + credibility 0.20 (3 consecutive) → pause.

    0.20 is below the paper floor (0.30); the paper-floor still bites
    on actually-degraded paper engines.
    """
    pool = _FakePool()
    pool.conn.fetchrow_queue.append(None)
    pool.conn.fetch_queue.append(
        [
            _confidence_row(0.20, datetime(2026, 5, 22 - i, 12, 0, tzinfo=UTC))
            for i in range(3)
        ]
    )
    with _stub_profile("reversion", LifecycleState.PAPER):
        paused = await check_credibility_drop(pool, engine="reversion")
    assert paused is True
    inserts = _app_log_inserts(pool.conn)
    events = [args[2] for (_, args) in inserts]
    assert ENGINE_CREDIBILITY_DROP_EVENT in events
    assert HELD_EVENT in events
    # Payload surfaces the applied floor + state for operator clarity.
    drop = next(
        args for (_, args) in inserts if args[2] == ENGINE_CREDIBILITY_DROP_EVENT
    )
    drop_payload = json.loads(drop[5])
    assert drop_payload["applied_floor_score"] == 30
    assert drop_payload["applied_lifecycle_state"] == "paper"


# ────────────────────────────────────────────────────────────────────────
# E7 — check_credibility_drop, LIVE mode-aware floor (0.60)
# ────────────────────────────────────────────────────────────────────────


async def test_e7_live_engine_credibility_0_45_pauses() -> None:
    """LIVE + credibility 0.45 (3 consecutive) → pause.

    Live-promoted engines retain the strict 0.60 floor; 0.45 below trips.
    """
    pool = _FakePool()
    pool.conn.fetchrow_queue.append(None)
    pool.conn.fetch_queue.append(
        [
            _confidence_row(0.45, datetime(2026, 5, 22 - i, 12, 0, tzinfo=UTC))
            for i in range(3)
        ]
    )
    with _stub_profile("live_engine", LifecycleState.LIVE):
        paused = await check_credibility_drop(pool, engine="live_engine")
    assert paused is True
    inserts = _app_log_inserts(pool.conn)
    drop = next(
        args for (_, args) in inserts if args[2] == ENGINE_CREDIBILITY_DROP_EVENT
    )
    drop_payload = json.loads(drop[5])
    assert drop_payload["applied_floor_score"] == 60
    assert drop_payload["applied_lifecycle_state"] == "live"


async def test_e7_live_engine_credibility_0_70_no_pause() -> None:
    """LIVE + credibility 0.70 (3 consecutive) → NO pause.

    Above the live floor (0.60); engine remains dispatchable.
    """
    pool = _FakePool()
    pool.conn.fetchrow_queue.append(None)
    pool.conn.fetch_queue.append(
        [
            _confidence_row(0.70, datetime(2026, 5, 22 - i, 12, 0, tzinfo=UTC))
            for i in range(3)
        ]
    )
    with _stub_profile("live_engine", LifecycleState.LIVE):
        paused = await check_credibility_drop(pool, engine="live_engine")
    assert paused is False
    assert _app_log_inserts(pool.conn) == []


# ────────────────────────────────────────────────────────────────────────
# E11 — check_lifecycle_degraded mirrors the same matrix
# ────────────────────────────────────────────────────────────────────────


async def test_e11_paper_engine_lifecycle_0_45_no_pause() -> None:
    """PAPER + lifecycle 0.45 (3 consecutive) → NO pause.

    N=3 explicitly (E11 default is 5; the brief's matrix lists 3 cycles).
    The paper floor (0.30) does not trip on 0.45.
    """
    pool = _FakePool()
    pool.conn.fetchrow_queue.append(None)
    pool.conn.fetch_queue.append(
        [
            _confidence_row(0.45, datetime(2026, 5, 22 - i, 12, 0, tzinfo=UTC))
            for i in range(3)
        ]
    )
    with _stub_profile("vector", LifecycleState.PAPER):
        paused = await check_lifecycle_degraded(
            pool, engine="vector", threshold=3,
        )
    assert paused is False
    assert _app_log_inserts(pool.conn) == []


async def test_e11_paper_engine_lifecycle_0_20_pauses() -> None:
    """PAPER + lifecycle 0.20 (3 consecutive) → pause."""
    pool = _FakePool()
    pool.conn.fetchrow_queue.append(None)
    pool.conn.fetch_queue.append(
        [
            _confidence_row(0.20, datetime(2026, 5, 22 - i, 12, 0, tzinfo=UTC))
            for i in range(3)
        ]
    )
    with _stub_profile("vector", LifecycleState.PAPER):
        paused = await check_lifecycle_degraded(
            pool, engine="vector", threshold=3,
        )
    assert paused is True
    inserts = _app_log_inserts(pool.conn)
    events = [args[2] for (_, args) in inserts]
    assert ENGINE_LIFECYCLE_DEGRADED_EVENT in events
    assert HELD_EVENT in events
    degraded = next(
        args for (_, args) in inserts if args[2] == ENGINE_LIFECYCLE_DEGRADED_EVENT
    )
    degraded_payload = json.loads(degraded[5])
    assert degraded_payload["applied_lifecycle_state"] == "paper"
    # PAPER floor is 0.30; the payload should reflect that.
    assert abs(degraded_payload["floor_pct"] - 0.30) < 1e-6


async def test_e11_live_engine_lifecycle_0_45_pauses() -> None:
    """LIVE + lifecycle 0.45 (3 consecutive) → pause."""
    pool = _FakePool()
    pool.conn.fetchrow_queue.append(None)
    pool.conn.fetch_queue.append(
        [
            _confidence_row(0.45, datetime(2026, 5, 22 - i, 12, 0, tzinfo=UTC))
            for i in range(3)
        ]
    )
    with _stub_profile("live_engine", LifecycleState.LIVE):
        paused = await check_lifecycle_degraded(
            pool, engine="live_engine", threshold=3,
        )
    assert paused is True
    inserts = _app_log_inserts(pool.conn)
    degraded = next(
        args for (_, args) in inserts if args[2] == ENGINE_LIFECYCLE_DEGRADED_EVENT
    )
    degraded_payload = json.loads(degraded[5])
    assert degraded_payload["applied_lifecycle_state"] == "live"
    assert abs(degraded_payload["floor_pct"] - 0.60) < 1e-6


async def test_e11_live_engine_lifecycle_0_70_no_pause() -> None:
    """LIVE + lifecycle 0.70 (3 consecutive) → NO pause."""
    pool = _FakePool()
    pool.conn.fetchrow_queue.append(None)
    pool.conn.fetch_queue.append(
        [
            _confidence_row(0.70, datetime(2026, 5, 22 - i, 12, 0, tzinfo=UTC))
            for i in range(3)
        ]
    )
    with _stub_profile("live_engine", LifecycleState.LIVE):
        paused = await check_lifecycle_degraded(
            pool, engine="live_engine", threshold=3,
        )
    assert paused is False
    assert _app_log_inserts(pool.conn) == []


# ────────────────────────────────────────────────────────────────────────
# Operator-protection — the four currently-paused engines all live in
# the 0.40-0.55 band; the mode-aware logic must NOT pause them now that
# they sit in PAPER. (One PAPER scenario per engine — the parametric
# table from the operator brief.)
# ────────────────────────────────────────────────────────────────────────


async def test_e7_currently_paused_paper_engines_no_longer_trip() -> None:
    """reversion 0.45 / vector 0.45 / momentum 0.55 / sentinel 0.40
    — all PAPER, all ABOVE the new paper floor (0.30) → no pause.

    This is the operator-visible delta after PR #272: the four pause-
    rows that bricked paper-trading must stop being emitted.
    """
    matrix = [
        ("reversion", 0.45),
        ("vector", 0.45),
        ("momentum", 0.55),
        ("sentinel", 0.40),
    ]
    for engine, credibility in matrix:
        pool = _FakePool()
        pool.conn.fetchrow_queue.append(None)
        pool.conn.fetch_queue.append(
            [
                _confidence_row(
                    credibility,
                    datetime(2026, 5, 22 - i, 12, 0, tzinfo=UTC),
                )
                for i in range(3)
            ]
        )
        with _stub_profile(engine, LifecycleState.PAPER):
            paused = await check_credibility_drop(pool, engine=engine)
        assert paused is False, (
            f"PAPER engine {engine} at credibility {credibility} should "
            f"NOT pause under the mode-aware paper floor (0.30)."
        )
        assert _app_log_inserts(pool.conn) == [], (
            f"PAPER engine {engine} should emit no pause events."
        )
