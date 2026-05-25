"""Tests for the P3 ``stage_then_promote_bars`` write path.

2026-05-25 trust-audit: prices_daily writes now flow through
``platform.prices_daily_staging`` BEFORE production:
  1. physical-truth filter (per-row) + quarantine reject path,
  2. bulk INSERT to staging tagged with staging_run_id,
  3. validate staging row count == accepted row count,
  4. promote via INSERT ... SELECT honoring the P4 provenance guard,
  5. mark staging rows promoted=true.

Hermetic — fake asyncpg pool records every SQL call so each phase
is independently pinnable.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from tpcore.data.ingest_alpaca_bars import (
    StagingValidationError,
    stage_then_promote_bars,
)

# ─────────────────────────────────────────────────────────────────────
# Fake pool — separates the staging INSERT / staged-count fetch /
# promote / mark-promoted / quarantine-INSERT call channels so tests
# can assert on each independently.
# ─────────────────────────────────────────────────────────────────────


class _FakeConn:
    def __init__(self, *, staged_count_override: int | None = None) -> None:
        self.executemany_calls: list[tuple[str, list]] = []
        self.execute_calls: list[tuple[str, tuple]] = []
        self.fetchval_calls: list[tuple[str, tuple]] = []
        self.quarantine_inserts: list[tuple] = []
        # If set, returned by the COUNT staged query — defaults to
        # "as many as were executemany'd" (happy path).
        self._staged_count_override = staged_count_override

    async def executemany(self, sql: str, rows: list) -> None:
        self.executemany_calls.append((sql, rows))

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.fetchval_calls.append((sql, args))
        if "INSERT INTO platform.ingest_quarantine" in sql:
            # The quarantine writer reads a returning id.
            self.quarantine_inserts.append(args)
            return uuid4()
        if "COUNT(*)" in sql and "prices_daily_staging" in sql:
            if self._staged_count_override is not None:
                return self._staged_count_override
            # Default: pretend everything we executemany'd landed.
            total = sum(len(rows) for _, rows in self.executemany_calls)
            return total
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self.execute_calls.append((sql, args))
        if "INSERT INTO platform.prices_daily" in sql and "SELECT" in sql:
            # Pretend every staged row promoted (happy path).
            total = sum(len(rows) for _, rows in self.executemany_calls)
            return f"INSERT 0 {total}"
        return "UPDATE 1"


class _AcquireCM:
    def __init__(self, conn): self._c = conn
    async def __aenter__(self): return self._c
    async def __aexit__(self, *e): return None


class _FakePool:
    def __init__(self, **conn_kwargs) -> None:
        self.conn = _FakeConn(**conn_kwargs)

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(self.conn)


def _bar(t_iso: str = "2026-05-20T05:00:00Z") -> dict:
    return {"t": t_iso, "o": 100.0, "h": 102.0, "l": 99.0, "c": 101.0, "v": 1000}


def _bad_bar_oob() -> dict:
    return {"t": "2026-05-20T05:00:00Z", "o": 1.0, "h": 1.0, "l": 1.0, "c": -1.0, "v": 100}


# ─────────────────────────────────────────────────────────────────────
# Happy path — physical-truth → stage → validate → promote → mark
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_phases_in_order() -> None:
    pool = _FakePool()
    rid: UUID = uuid4()
    bars = [_bar("2026-05-20T05:00:00Z"), _bar("2026-05-21T05:00:00Z")]
    promoted = await stage_then_promote_bars(
        pool, "AAPL", bars,
        staging_run_id=rid, source="fmp",
    )
    assert promoted == 2
    # Phase 2: executemany on staging INSERT
    assert len(pool.conn.executemany_calls) == 1
    stage_sql, stage_rows = pool.conn.executemany_calls[0]
    assert "INSERT INTO platform.prices_daily_staging" in stage_sql
    assert len(stage_rows) == 2
    assert all(row[0] == rid for row in stage_rows)
    # Phase 3: COUNT(*) verification
    assert any("COUNT(*)" in c[0] for c in pool.conn.fetchval_calls)
    # Phase 4: INSERT ... SELECT promote
    promote = next(
        c for c in pool.conn.execute_calls
        if "INSERT INTO platform.prices_daily" in c[0] and "SELECT" in c[0]
    )
    assert promote[1][0] == rid
    assert promote[1][1] == "AAPL"
    # Phase 5: mark promoted=true
    mark = next(
        c for c in pool.conn.execute_calls
        if "UPDATE platform.prices_daily_staging" in c[0]
        and "promoted = true" in c[0]
    )
    assert mark[1][0] == rid
    assert mark[1][1] == "AAPL"


@pytest.mark.asyncio
async def test_promote_sql_carries_source_priority_where_clause() -> None:
    """The promote SQL inherits the P4 provenance-downgrade guard."""
    pool = _FakePool()
    await stage_then_promote_bars(
        pool, "AAPL", [_bar()],
        staging_run_id=uuid4(), source="fmp",
    )
    promote = next(
        c for c in pool.conn.execute_calls
        if "INSERT INTO platform.prices_daily" in c[0] and "SELECT" in c[0]
    )
    assert "_source_priority(EXCLUDED.source)" in promote[0]
    assert "_source_priority(platform.prices_daily.source)" in promote[0]


# ─────────────────────────────────────────────────────────────────────
# Bad rows route to quarantine
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_physical_truth_reject_routes_to_quarantine() -> None:
    pool = _FakePool()
    bars = [_bar(), _bad_bar_oob()]  # 1 good, 1 bad
    promoted = await stage_then_promote_bars(
        pool, "AAPL", bars,
        staging_run_id=uuid4(), source="fmp",
    )
    # Only the good bar reaches staging.
    _, stage_rows = pool.conn.executemany_calls[0]
    assert len(stage_rows) == 1
    # Bad bar quarantined with the canonical feed source.
    assert len(pool.conn.quarantine_inserts) == 1
    q_args = pool.conn.quarantine_inserts[0]
    assert q_args[0] == "fmp_daily_bars"
    assert q_args[1] == "platform.prices_daily"
    assert q_args[4] == "validation"
    assert promoted == 1


# ─────────────────────────────────────────────────────────────────────
# Staging validation failure blocks promote
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_staging_validation_failure_blocks_promote() -> None:
    """A staged_count mismatch (e.g. silent in-batch duplicate) raises
    StagingValidationError BEFORE the promote SQL fires."""
    # Override staged-count to a value < the executemany count: that's
    # how an in-batch dup would surface (ON CONFLICT DO NOTHING).
    pool = _FakePool(staged_count_override=0)  # nothing landed
    with pytest.raises(StagingValidationError, match="staging row count mismatch"):
        await stage_then_promote_bars(
            pool, "AAPL", [_bar(), _bar("2026-05-21T05:00:00Z")],
            staging_run_id=uuid4(), source="fmp",
        )
    # Promote SQL must NOT have fired.
    assert not any(
        "INSERT INTO platform.prices_daily" in c[0] and "SELECT" in c[0]
        for c in pool.conn.execute_calls
    )
    # Mark-promoted SQL must NOT have fired.
    assert not any(
        "UPDATE platform.prices_daily_staging" in c[0]
        and "promoted = true" in c[0]
        for c in pool.conn.execute_calls
    )


# ─────────────────────────────────────────────────────────────────────
# Empty input
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_bars_no_db_writes() -> None:
    pool = _FakePool()
    promoted = await stage_then_promote_bars(
        pool, "AAPL", [], staging_run_id=uuid4(), source="fmp",
    )
    assert promoted == 0
    assert pool.conn.executemany_calls == []
    assert pool.conn.execute_calls == []
    assert pool.conn.fetchval_calls == []


@pytest.mark.asyncio
async def test_all_bars_rejected_no_staging_no_promote() -> None:
    pool = _FakePool()
    promoted = await stage_then_promote_bars(
        pool, "AAPL", [_bad_bar_oob(), _bad_bar_oob()],
        staging_run_id=uuid4(), source="fmp",
    )
    assert promoted == 0
    assert pool.conn.executemany_calls == []
    # No promote SQL.
    assert not any(
        "INSERT INTO platform.prices_daily" in c[0] and "SELECT" in c[0]
        for c in pool.conn.execute_calls
    )
    # But quarantine got both.
    assert len(pool.conn.quarantine_inserts) == 2
