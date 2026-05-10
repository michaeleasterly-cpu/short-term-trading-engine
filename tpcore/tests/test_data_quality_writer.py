"""Tests for ``tpcore.quality.data_quality.DataQualityWriter`` against a fake asyncpg pool."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tpcore.quality.data_quality import DataQualityScore, DataQualityWriter

# ────────────────────────────────────────────────────────────────────────────
# Fake pool — same shape as test_aar_writer.py / test_persistent_store.py
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


def _score(source: str = "validation.delistings") -> DataQualityScore:
    return DataQualityScore(
        source=source,
        timestamp=datetime(2026, 5, 10, 6, 0, tzinfo=UTC),
        latency_ms=42,
        missing_bars=0,
        stale=False,
        confidence=Decimal("1.000"),
        notes='{"failures": []}',
    )


# ────────────────────────────────────────────────────────────────────────────
# write
# ────────────────────────────────────────────────────────────────────────────


async def test_write_no_pool_returns_false() -> None:
    """Without a pool, the writer is a no-op (DB not wired)."""
    assert await DataQualityWriter(db_pool=None).write(_score()) is False


async def test_write_inserts_new_row_returns_true() -> None:
    pool = _FakePool(fetchrow_result={"?column?": 1})
    wrote = await DataQualityWriter(pool).write(_score())
    assert wrote is True
    assert len(pool.conn.calls) == 1
    sql, args = pool.conn.calls[0]
    assert "INSERT INTO platform.data_quality_log" in sql
    assert "ON CONFLICT" in sql
    assert "DO NOTHING" in sql


async def test_write_idempotent_on_conflict_returns_false() -> None:
    """Second call with same (source, timestamp) returns False (no row written)."""
    pool = _FakePool(fetchrow_result=None)  # asyncpg returns None when RETURNING produces no row
    wrote = await DataQualityWriter(pool).write(_score())
    assert wrote is False


async def test_write_passes_score_fields() -> None:
    """All scalar fields land in the right SQL parameter slots."""
    pool = _FakePool(fetchrow_result={"?column?": 1})
    score = DataQualityScore(
        source="validation.splits",
        timestamp=datetime(2026, 5, 10, 6, 0, tzinfo=UTC),
        latency_ms=123,
        missing_bars=2,
        stale=True,
        confidence=Decimal("0.800"),
        notes='{"failures": [{"ticker": "AAPL"}]}',
    )
    await DataQualityWriter(pool).write(score)
    _, args = pool.conn.calls[0]
    assert "validation.splits" in args
    assert datetime(2026, 5, 10, 6, 0, tzinfo=UTC) in args
    assert 123 in args
    assert 2 in args
    assert True in args
    assert Decimal("0.800") in args
    assert '{"failures": [{"ticker": "AAPL"}]}' in args
