"""Tests for ``tpcore.ingestion.quarantine`` — ``platform.ingest_quarantine``
writer + the wiring into ``_upsert_bars``.

P5 trust-audit (2026-05-25): the quarantine table existed schema-only.
This module pins the writer contract + the wiring that routes
physical-truth-rejected bars to the table instead of silently
counting them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from tpcore.ingestion import quarantine

# ─────────────────────────────────────────────────────────────────────
# Fake asyncpg pool
# ─────────────────────────────────────────────────────────────────────


class _RecordingConn:
    def __init__(self, *, raise_on_insert: bool = False) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._raise = raise_on_insert
        self._next_id = uuid4()

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        if self._raise:
            raise RuntimeError("simulated quarantine write failure")
        return self._next_id

    async def executemany(self, _sql: str, _rows: list[tuple]) -> None:
        # _upsert_bars uses executemany for the accepted-row writes.
        return None


class _AcquireCM:
    def __init__(self, conn: _RecordingConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _RecordingConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakePool:
    def __init__(self, **conn_kwargs: Any) -> None:
        self.conn = _RecordingConn(**conn_kwargs)

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(self.conn)


# ─────────────────────────────────────────────────────────────────────
# record_rejection unit tests
# ─────────────────────────────────────────────────────────────────────


class TestRecordRejection:
    @pytest.mark.asyncio
    async def test_inserts_with_expected_columns(self) -> None:
        pool = _FakePool()
        qid = await quarantine.record_rejection(
            pool,
            source="fmp_daily_bars",
            target_table="platform.prices_daily",
            payload={"ticker": "AAPL", "t": "2026-05-22T05:00:00Z", "c": -1},
            error_message="ohlc_out_of_range",
            error_kind=quarantine.ERROR_VALIDATION,
        )
        assert qid is not None
        assert len(pool.conn.calls) == 1
        sql, args = pool.conn.calls[0]
        assert "INSERT INTO platform.ingest_quarantine" in sql
        assert args[0] == "fmp_daily_bars"
        assert args[1] == "platform.prices_daily"
        # payload is JSON-encoded
        import json as _json
        assert _json.loads(args[2])["ticker"] == "AAPL"
        assert args[3] == "ohlc_out_of_range"
        assert args[4] == "validation"
        assert args[5] is None  # no manifest_id

    @pytest.mark.asyncio
    async def test_carries_manifest_id_when_supplied(self) -> None:
        pool = _FakePool()
        mid = uuid4()
        await quarantine.record_rejection(
            pool,
            source="fmp_daily_bars",
            target_table="platform.prices_daily",
            payload={"ticker": "X"},
            error_message="x",
            manifest_id=mid,
        )
        _, args = pool.conn.calls[0]
        assert args[5] == mid

    @pytest.mark.asyncio
    async def test_swallows_write_failures(self) -> None:
        """A failed quarantine INSERT is best-effort: log + return None.
        The producer's primary error path must not be masked by an
        audit-write blow-up."""
        pool = _FakePool(raise_on_insert=True)
        qid = await quarantine.record_rejection(
            pool,
            source="fmp_daily_bars",
            target_table="platform.prices_daily",
            payload={"x": 1},
            error_message="oops",
        )
        assert qid is None

    @pytest.mark.asyncio
    async def test_payload_with_non_json_types_serializes(self) -> None:
        """Decimals, dates, datetimes survive via default=str."""
        from datetime import UTC, datetime
        from datetime import date as _date_t
        pool = _FakePool()
        await quarantine.record_rejection(
            pool,
            source="alpaca_daily_bars",
            target_table="platform.prices_daily",
            payload={
                "ticker": "MSFT",
                "ratio": Decimal("1168.5"),
                "date": _date_t(2026, 5, 22),
                "recorded_at": datetime(2026, 5, 22, 14, tzinfo=UTC),
            },
            error_message="ratio_implausible",
        )
        # Did not raise; INSERT received a JSON string.
        _, args = pool.conn.calls[0]
        import json as _json
        body = _json.loads(args[2])
        assert body["ratio"] == "1168.5"
        assert body["date"] == "2026-05-22"

    @pytest.mark.asyncio
    async def test_unknown_error_kind_rejected_at_producer(self) -> None:
        pool = _FakePool()
        with pytest.raises(ValueError, match="error_kind"):
            await quarantine.record_rejection(
                pool,
                source="fmp_daily_bars",
                target_table="platform.prices_daily",
                payload={"x": 1},
                error_message="x",
                error_kind="not_a_known_kind",
            )
        # No INSERT happened.
        assert pool.conn.calls == []


def test_known_error_kinds_exhaustive() -> None:
    """KNOWN_ERROR_KINDS must match the DB CHECK constraint enum (per
    migration 20260525_0200)."""
    assert quarantine.KNOWN_ERROR_KINDS == frozenset({
        "parse", "validation", "fk_violation",
        "unique_violation", "check_violation",
        "type_coercion", "other",
    })


# ─────────────────────────────────────────────────────────────────────
# _upsert_bars wiring — rejected bars land in quarantine
# ─────────────────────────────────────────────────────────────────────


class _AcceptOrCountConn:
    """Records quarantine INSERTs separately from accepted-row executemany."""
    def __init__(self) -> None:
        self.quarantine_inserts: list[tuple] = []
        self.executemany_calls: int = 0
        self._next_id = uuid4()

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "INSERT INTO platform.ingest_quarantine" in sql:
            self.quarantine_inserts.append(args)
            return self._next_id
        return None

    async def executemany(self, _sql: str, rows: list[tuple]) -> None:
        self.executemany_calls += 1
        # Count rows for the test
        self.executemany_rows = list(rows)


class _AcceptOrCountAcquireCM:
    def __init__(self, conn: _AcceptOrCountConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _AcceptOrCountConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _AcceptOrCountPool:
    def __init__(self) -> None:
        self.conn = _AcceptOrCountConn()

    def acquire(self) -> _AcceptOrCountAcquireCM:
        return _AcceptOrCountAcquireCM(self.conn)


def _good_bar(t_iso: str = "2026-05-20T05:00:00Z") -> dict:
    return {"t": t_iso, "o": 100.0, "h": 102.0, "l": 99.0, "c": 101.0, "v": 1000}


def _bad_bar_oob() -> dict:
    """Close out of range — rejected by physical-truth gate."""
    return {"t": "2026-05-20T05:00:00Z", "o": 1.0, "h": 1.0, "l": 1.0, "c": -1.0, "v": 100}


def _bad_bar_ohlc_inconsistent() -> dict:
    return {"t": "2026-05-20T05:00:00Z", "o": 5.0, "h": 1.0, "l": 99.0, "c": 1.5, "v": 100}


@pytest.mark.asyncio
async def test_rejected_bar_lands_in_quarantine_accepted_bar_upserts() -> None:
    """A mixed batch: one good bar, two bad. The good one reaches
    executemany; both bad ones produce quarantine rows with feed-
    attributed source + reasons."""
    from tpcore.data.ingest_alpaca_bars import _upsert_bars
    pool = _AcceptOrCountPool()
    bars = [_good_bar(), _bad_bar_oob(), _bad_bar_ohlc_inconsistent()]
    inserted = await _upsert_bars(
        pool, "AAPL", bars, delisted=False, source="fmp",
    )
    # One good bar was executemany'd.
    assert inserted == 1
    assert pool.conn.executemany_calls == 1
    assert len(pool.conn.executemany_rows) == 1
    # Two quarantine inserts.
    assert len(pool.conn.quarantine_inserts) == 2
    # Both carry the canonical feed source.
    for q_args in pool.conn.quarantine_inserts:
        assert q_args[0] == "fmp_daily_bars"
        assert q_args[1] == "platform.prices_daily"
        assert q_args[4] == "validation"
    # Reasons are distinct (one ohlc_out_of_range, one ohlc_inconsistent).
    reasons = [q_args[3] for q_args in pool.conn.quarantine_inserts]
    assert any("ohlc_out_of_range" in r for r in reasons)
    assert any("ohlc_inconsistent" in r for r in reasons)


@pytest.mark.asyncio
async def test_legacy_alpaca_source_attributed_to_alpaca_daily_bars() -> None:
    from tpcore.data.ingest_alpaca_bars import _upsert_bars
    pool = _AcceptOrCountPool()
    await _upsert_bars(
        pool, "AAPL", [_bad_bar_oob()], delisted=False, source="alpaca",
    )
    assert pool.conn.quarantine_inserts[0][0] == "alpaca_daily_bars"
