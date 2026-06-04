"""Tests for ``tpcore.quality.data_quality.DataQualityWriter`` against a fake asyncpg pool."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tpcore.quality.data_quality import (
    KIND_CONFIRMED_DATA_GAP_EVIDENCE,
    KIND_PARITY_DRIFT,
    DataQualityScore,
    DataQualityWriter,
    write_row,
)

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
    # Plan 2 redesign: kind discriminator is bound as $1 (not inlined); the
    # canonical write_row path stamps 'validation' for the writer. notes cast to jsonb.
    assert args[0] == "validation"
    assert "$8::jsonb" in sql
    # The old UNIQUE(source, timestamp) was dropped → no ON CONFLICT anymore.
    assert "ON CONFLICT" not in sql


async def test_write_no_row_written_returns_false() -> None:
    """When RETURNING yields no row (no pool wrote it), write() returns False."""
    pool = _FakePool(fetchrow_result=None)  # asyncpg returns None when RETURNING produces no row
    wrote = await DataQualityWriter(pool).write(_score())
    assert wrote is False


async def test_write_wraps_plain_text_notes_as_jsonb() -> None:
    """Free-text notes (not valid JSON) are wrapped as {"text": ...} so the
    jsonb column stays valid."""
    pool = _FakePool(fetchrow_result={"?column?": 1})
    score = DataQualityScore(
        source="validation.freshness",
        timestamp=datetime(2026, 5, 10, 6, 0, tzinfo=UTC),
        latency_ms=1,
        missing_bars=0,
        stale=False,
        confidence=Decimal("1.000"),
        notes="stale by 3 days",  # plain text, NOT JSON
    )
    await DataQualityWriter(pool).write(score)
    _, args = pool.conn.calls[0]
    assert '{"text": "stale by 3 days"}' in args


async def test_write_passes_through_valid_json_notes() -> None:
    """Already-valid JSON notes pass through unchanged (downstream JSON readers)."""
    pool = _FakePool(fetchrow_result={"?column?": 1})
    score = _score()  # notes='{"failures": []}' is valid JSON
    await DataQualityWriter(pool).write(score)
    _, args = pool.conn.calls[0]
    assert '{"failures": []}' in args


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


# ────────────────────────────────────────────────────────────────────────────
# write_row CHECK-violation guard (dql_typed_cols_validation_only)
# ────────────────────────────────────────────────────────────────────────────


async def test_write_row_rejects_unknown_kind() -> None:
    """An unrecognised ``kind`` is rejected loudly before any DB call."""
    pool = _FakePool(fetchrow_result={"?column?": 1})
    with pytest.raises(ValueError, match="unknown data_quality_log kind"):
        await write_row(
            pool,
            kind="not_a_real_kind",
            source="x",
            timestamp=datetime(2026, 5, 10, 6, 0, tzinfo=UTC),
            notes=None,
        )
    assert pool.conn.calls == []


async def test_write_row_rejects_typed_cols_on_non_validation_kind() -> None:
    """A non-``validation`` kind passed a typed metric column raises ValueError
    locally (mirrors the ``dql_typed_cols_validation_only`` DB CHECK) — failing
    loud rather than tripping the CHECK at runtime. No DB call is made."""
    pool = _FakePool(fetchrow_result={"?column?": 1})
    with pytest.raises(ValueError, match="forbids typed metric columns"):
        await write_row(
            pool,
            kind=KIND_PARITY_DRIFT,
            source="parity.x",
            timestamp=datetime(2026, 5, 10, 6, 0, tzinfo=UTC),
            notes={"drift_bps": 3},
            confidence=0.5,
        )
    assert pool.conn.calls == []


async def test_write_row_confirmed_data_gap_evidence_with_notes_only_ok() -> None:
    """The confirmed-data-gap kind with notes-only (no typed cols) is accepted
    and produces exactly one INSERT under the new kind discriminator."""
    pool = _FakePool(fetchrow_result={"?column?": 1})
    wrote = await write_row(
        pool,
        kind=KIND_CONFIRMED_DATA_GAP_EVIDENCE,
        source="confirmed_data_gap_evidence.sec_companyfacts",
        timestamp=datetime(2026, 5, 10, 6, 0, tzinfo=UTC),
        notes={"ticker": "AAPL", "outcome": "extract_none"},
    )
    assert wrote is True
    assert len(pool.conn.calls) == 1
    _, args = pool.conn.calls[0]
    assert args[0] == KIND_CONFIRMED_DATA_GAP_EVIDENCE
