"""Tests for ``tpcore.aar.writer.AARWriter`` against a fake asyncpg pool."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tpcore.aar.models import AfterActionReport, ExitReason
from tpcore.aar.writer import AARWriter

# ────────────────────────────────────────────────────────────────────────────
# Fake pool (same shape as test_persistent_store.py — kept local on purpose)
# ────────────────────────────────────────────────────────────────────────────


class _FakeConn:
    def __init__(self, fetchrow_result: object = None) -> None:
        self.fetchrow_result = fetchrow_result
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args) -> object:
        self.calls.append((sql, args))
        return self.fetchrow_result


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(self, fetchrow_result: object = None) -> None:
        self.conn = _FakeConn(fetchrow_result=fetchrow_result)

    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(self.conn)


def _aar(trade_id: str = "sigma-AAPL-001", exit_reason: ExitReason = ExitReason.TIER1_MID_BAND) -> AfterActionReport:
    return AfterActionReport(
        engine="sigma",
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
    sql, args = pool.conn.calls[0]
    assert "INSERT INTO platform.aar_events" in sql
    assert "ON CONFLICT (engine, trade_id) DO NOTHING" in sql
    assert args[0] == "sigma"
    assert args[1] == "sigma-AAPL-001"
    assert args[2] == "AAPL"
    # aar_data is the model serialized as JSON; cast to jsonb in SQL.
    payload = json.loads(args[3])
    assert payload["engine"] == "sigma"
    assert payload["exit_reason"] == "tier1_mid_band"


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
    from tpcore.db import build_asyncpg_pool

    pool = await build_asyncpg_pool(os.environ["DATABASE_URL"])
    try:
        writer = AARWriter(pool)
        aar = _aar(trade_id="sigma-integration-AAR-001")
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM platform.aar_events WHERE engine=$1 AND trade_id=$2",
                aar.engine,
                aar.trade_id,
            )
        # First call writes.
        assert await writer.write_aar(aar) is True
        # Idempotent re-write: same (engine, trade_id) → conflict.
        assert await writer.write_aar(aar) is False
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT engine, trade_id, ticker FROM platform.aar_events "
                "WHERE engine=$1 AND trade_id=$2",
                aar.engine,
                aar.trade_id,
            )
            assert row is not None
            assert row["ticker"] == "AAPL"
            await conn.execute(
                "DELETE FROM platform.aar_events WHERE engine=$1 AND trade_id=$2",
                aar.engine,
                aar.trade_id,
            )
    finally:
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
