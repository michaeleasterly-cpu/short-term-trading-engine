"""Tests for ``tpcore.aar.writer.AARWriter`` against a fake asyncpg pool."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tpcore.aar.models import AfterActionReport, ExitReason
from tpcore.aar.writer import AARWriter
from tpcore.identity.dispatcher import IdentityDispatcher


@pytest.fixture(autouse=True)
def _reset_dispatcher_cache() -> None:
    """The IdentityDispatcher's class-level ``_shared_caches`` is keyed
    on ``id(pool)``. Python recycles object ids after GC, so a fake
    pool created here can land on a cache key a prior test populated
    with a different fetchval result. The dispatcher's caller (
    ``AARWriter``) then returns the stale None instead of the
    fetchval value the test set up.

    Order-dependent failure observed in CI under the AUTHORITATIVE
    serial gate (the local parallel run masks it). Scoped to this
    file rather than a global autouse to avoid the ordering-shift
    side effect documented in
    ``tpcore/tests/conftest.py`` (the global autouse was removed
    because it surfaced an unrelated fragility).
    """
    IdentityDispatcher.reset_shared_caches()

# ────────────────────────────────────────────────────────────────────────────
# Fake pool (same shape as test_persistent_store.py — kept local on purpose)
# ────────────────────────────────────────────────────────────────────────────


class _FakeConn:
    def __init__(
        self,
        fetchrow_result: object = None,
        fetchval_result: object = None,
    ) -> None:
        self.fetchrow_result = fetchrow_result
        # PR-12 (2026-05-25): AARWriter now uses IdentityDispatcher to
        # resolve ticker→classification_id at write time; that runs a
        # fetchval against ticker_history. ``fetchval_result`` is the
        # cid the fake returns (default None ⇒ ticker not in history,
        # which is acceptable — aar_events.classification_id is nullable).
        self.fetchval_result = fetchval_result
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args) -> object:
        self.calls.append((sql, args))
        return self.fetchrow_result

    async def fetchval(self, sql: str, *args) -> object:
        self.calls.append((sql, args))
        return self.fetchval_result


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(
        self,
        fetchrow_result: object = None,
        fetchval_result: object = None,
    ) -> None:
        self.conn = _FakeConn(
            fetchrow_result=fetchrow_result,
            fetchval_result=fetchval_result,
        )

    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(self.conn)


def _aar(
    trade_id: str = "sigma-AAPL-001",
    exit_reason: ExitReason = ExitReason.TIER1_MID_BAND,
    ticker: str = "AAPL",
) -> AfterActionReport:
    return AfterActionReport(
        engine="sigma",
        trade_id=trade_id,
        ticker=ticker,
        entry_ts=datetime(2026, 5, 9, 13, 30, tzinfo=UTC),
        exit_ts=datetime(2026, 5, 9, 19, 55, tzinfo=UTC),
        entry_price=Decimal("180.00"),
        exit_price=Decimal("184.00"),
        qty=Decimal("4"),
        confidence_at_entry=Decimal("0.80"),
        sizing_pct_of_engine_equity=Decimal("0.144"),
        pnl_gross=Decimal("16.00"),
        pnl_net=Decimal("16.00"),
        exit_reason=exit_reason,
        rule_compliance=True,
    )


# ────────────────────────────────────────────────────────────────────────────
# write_aar
# ────────────────────────────────────────────────────────────────────────────


async def test_write_aar_no_pool_returns_false() -> None:
    """Without a pool, the writer is a no-op (DB not wired)."""
    assert await AARWriter(db_pool=None).write_aar(_aar()) is False


async def test_write_aar_inserts_new_row_returns_true() -> None:
    pool = _FakePool(fetchrow_result={"?column?": 1})
    wrote = await AARWriter(pool).write_aar(_aar())
    assert wrote is True
    # The dispatcher's fetchval (ticker → classification_id) ran first;
    # the INSERT fetchrow is the second call. Find it.
    insert_call = next(c for c in pool.conn.calls if "INSERT INTO platform.aar_events" in c[0])
    sql, args = insert_call
    assert "ON CONFLICT (engine, trade_id) DO NOTHING" in sql
    assert "classification_id" in sql
    assert args[0] == "sigma"
    assert args[1] == "sigma-AAPL-001"
    assert args[2] == "AAPL"
    assert args[3] is None  # dispatcher returned None (fake fetchval_result default)
    # aar_data is the model serialized as JSON; cast to jsonb in SQL.
    payload = json.loads(args[4])
    assert payload["engine"] == "sigma"
    assert payload["exit_reason"] == "tier1_mid_band"


async def test_write_aar_populates_classification_id_when_dispatcher_resolves() -> None:
    """When IdentityDispatcher returns a cid, the writer persists it."""
    pool = _FakePool(
        fetchrow_result={"?column?": 1},
        fetchval_result="USOZ80NAAPL456",
    )
    wrote = await AARWriter(pool).write_aar(_aar())
    assert wrote is True
    insert_call = next(c for c in pool.conn.calls if "INSERT INTO platform.aar_events" in c[0])
    _, args = insert_call
    assert args[3] == "USOZ80NAAPL456"


async def test_write_aar_idempotent_on_conflict_returns_false() -> None:
    """Conflict path — second call sees ``RETURNING 1`` produce no row."""
    pool = _FakePool(fetchrow_result=None)
    wrote = await AARWriter(pool).write_aar(_aar())
    assert wrote is False


@pytest.mark.skipif(
    not os.environ.get("RUN_DB_INTEGRATION_TESTS"),
    reason="RUN_DB_INTEGRATION_TESTS not set",
)
async def test_aar_writer_integration_roundtrip() -> None:
    """Optional: real DB roundtrip — inserts, reads back, cleans up."""
    from datetime import UTC
    from datetime import datetime as _dt

    from tpcore.db import build_asyncpg_pool
    from tpcore.identity.tkr14 import (
        AssetClass,
        DiscoverySource,
        IPOVenue,
        mint,
    )

    pool = await build_asyncpg_pool(os.environ["DATABASE_URL"])
    # Dedicated fixture ticker — NOT a real symbol — so the seeded identity
    # window never collides with live ticker_history rows (the EXCLUDE
    # constraint on ticker would RAISE, not skip, on an overlapping window).
    test_ticker = "ZZZAAR"
    # v2.2 P5: ticker_classifications.id is NOT NULL (TKR-14 stable identity).
    test_tkr14 = mint(
        country="US",
        asset_class=AssetClass.STOCK,
        ipo_venue=IPOVenue.OTHER,
        discovery_source=DiscoverySource.OTHER,
        cik=None,
        legal_name="ZZZAAR Inc. (test fixture)",
        now=_dt(2020, 1, 1, tzinfo=UTC),
    )
    writer = AARWriter(pool)
    aar = _aar(trade_id="sigma-integration-AAR-001", ticker=test_ticker)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM platform.aar_events WHERE engine=$1 AND trade_id=$2",
                aar.engine,
                aar.trade_id,
            )
            # The identity-contract resolver (hard mode) reads ticker_history,
            # NOT ticker_classifications. aar_events.classification_id is NOT
            # NULL under the hard contract, and the BEFORE INSERT trigger
            # resolves on recorded_at::date (= write-time now()). Seed BOTH a
            # classifications row (FK target for ticker_history) AND an open
            # SCD-2 window covering now so the resolver windows the write.
            await conn.execute(
                """
                INSERT INTO platform.ticker_classifications
                    (id, ticker, current_ticker, asset_class, source, lifetime_start)
                VALUES ($1, $2, $2, 'stock', 'test_fixture', DATE '2000-01-01')
                ON CONFLICT (id) DO NOTHING
                """,
                test_tkr14, test_ticker,
            )
            await conn.execute(
                """
                INSERT INTO platform.ticker_history
                    (classification_id, ticker, valid_from, valid_to)
                VALUES ($1, $2, DATE '2000-01-01', NULL)
                ON CONFLICT (classification_id, valid_from) DO NOTHING
                """,
                test_tkr14, test_ticker,
            )
        # First call writes.
        assert await writer.write_aar(aar) is True
        # Idempotent re-write: same (engine, trade_id) → conflict.
        assert await writer.write_aar(aar) is False
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT engine, trade_id, ticker, classification_id "
                "FROM platform.aar_events WHERE engine=$1 AND trade_id=$2",
                aar.engine,
                aar.trade_id,
            )
            assert row is not None
            assert row["ticker"] == test_ticker
            # The hard contract populated classification_id from the window.
            assert row["classification_id"] == test_tkr14
            await conn.execute(
                "DELETE FROM platform.aar_events WHERE engine=$1 AND trade_id=$2",
                aar.engine,
                aar.trade_id,
            )
    finally:
        # FK-safe teardown order: aar_events (already deleted in the happy
        # path; repeat for the failure path) → ticker_history → then
        # ticker_classifications (ON DELETE RESTRICT on both FKs).
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM platform.aar_events WHERE engine=$1 AND trade_id=$2",
                aar.engine,
                aar.trade_id,
            )
            await conn.execute(
                "DELETE FROM platform.ticker_history WHERE ticker=$1 AND classification_id=$2",
                test_ticker, test_tkr14,
            )
            await conn.execute(
                "DELETE FROM platform.ticker_classifications "
                "WHERE ticker=$1 AND source='test_fixture'",
                test_ticker,
            )
        await pool.close()


# ── pool property (added 2026-05-14) ───────────────────────────────────


def test_aar_writer_pool_property_returns_none_when_unwired():
    """A db_pool=None construction → pool property returns None.
    Mirrors what consumers see in tests / DB-less environments."""
    from tpcore.aar.writer import AARWriter

    writer = AARWriter(db_pool=None)
    assert writer.pool is None


def test_aar_writer_pool_property_returns_underlying_pool():
    """Whatever db_pool was passed at construction is exposed via
    .pool — order managers use this instead of reaching into _pool."""
    from tpcore.aar.writer import AARWriter

    sentinel = object()  # any non-None object — the property is a passthrough
    writer = AARWriter(db_pool=sentinel)  # type: ignore[arg-type]
    assert writer.pool is sentinel
