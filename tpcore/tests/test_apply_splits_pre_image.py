"""Tests for the P4 destructive-write protection on ``apply_split``.

2026-05-25 trust-audit: before this PR ``apply_split`` performed an
``UPDATE platform.prices_daily`` with NO pre-image / diff / audit
trail — a bad split factor would silently rewrite historical bars.
Now: every call writes a row to ``platform.split_pre_image_log``
BEFORE the UPDATE and flips it to ``applied=true`` after; abnormal
splits (too many rows, implausible ratio) are rejected without a
DB write.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from tpcore.data import apply_splits

# ─────────────────────────────────────────────────────────────────────
# Fake asyncpg pool — records ALL conn.fetch / fetchval / execute calls
# in invocation order so the tests can pin the pre-image-before-update
# ordering.
# ─────────────────────────────────────────────────────────────────────


class _FakeConn:
    def __init__(self, *, bars: list[dict], n_to_update: int = 100) -> None:
        self.calls: list[tuple[str, str, tuple]] = []
        self._bars = bars
        self._n_to_update = n_to_update
        self._pre_image_id = uuid4()

    async def fetch(self, sql: str, *args):
        self.calls.append(("fetch", sql, args))
        if "ORDER BY date DESC" in sql and "LIMIT 2" in sql:
            return list(self._bars)
        if "LIMIT 5" in sql:
            # sample rows
            return [{"date": "2020-08-28", "open": "500", "high": "510",
                     "low": "498", "close": "500", "adjusted_close": "500",
                     "volume": "100000"}]
        return []

    async def fetchval(self, sql: str, *args):
        self.calls.append(("fetchval", sql, args))
        if "COUNT(*)" in sql:
            return self._n_to_update
        if "INSERT INTO platform.split_pre_image_log" in sql:
            return self._pre_image_id
        return None

    async def execute(self, sql: str, *args):
        self.calls.append(("execute", sql, args))
        if "UPDATE platform.prices_daily" in sql:
            # Pretend the update touched the planned row count.
            return f"UPDATE {self._n_to_update}"
        return "UPDATE 1"


class _AcquireCM:
    def __init__(self, conn): self._conn = conn
    async def __aenter__(self): return self._conn
    async def __aexit__(self, *exc): return None


class _FakePool:
    def __init__(self, **conn_kwargs) -> None:
        self.conn = _FakeConn(**conn_kwargs)

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(self.conn)


def _bars_around_split(*, before_close: str, after_close: str) -> list[dict]:
    """Two rows: action_date row first, then the prior-day row."""
    return [
        {"date": date(2020, 8, 31), "close": Decimal(after_close)},
        {"date": date(2020, 8, 28), "close": Decimal(before_close)},
    ]


# ─────────────────────────────────────────────────────────────────────
# Happy path — pre-image written BEFORE the UPDATE, mark applied AFTER
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pre_image_written_before_update_and_marked_applied() -> None:
    pool = _FakePool(
        bars=_bars_around_split(before_close="500", after_close="125"),
        n_to_update=120,
    )
    result = await apply_splits.apply_split(
        pool, "AAPL", date(2020, 8, 31), Decimal("4"),
    )
    assert result["applied"] is True
    assert result["n_rows_updated"] == 120
    assert "pre_image_id" in result

    # Required ordering:
    #   fetch (around-split bars)
    #   → fetchval (count rows to update)
    #   → fetch (sample rows)
    #   → fetchval (INSERT pre_image, RETURNING id)
    #   → execute (UPDATE prices_daily)
    #   → execute (mark applied=true)
    kinds = [(c[0], c[1].strip().split()[0].upper()) for c in pool.conn.calls]
    # Find the indices of the pre-image INSERT, the UPDATE, and the
    # applied flip.
    insert_idx = next(
        i for i, c in enumerate(pool.conn.calls)
        if "INSERT INTO platform.split_pre_image_log" in c[1]
    )
    update_idx = next(
        i for i, c in enumerate(pool.conn.calls)
        if "UPDATE platform.prices_daily" in c[1]
    )
    applied_idx = next(
        i for i, c in enumerate(pool.conn.calls)
        if "UPDATE platform.split_pre_image_log" in c[1]
        and "applied = true" in c[1]
    )
    assert insert_idx < update_idx < applied_idx, kinds


@pytest.mark.asyncio
async def test_pre_image_records_actual_row_count() -> None:
    """Mark-applied UPDATE passes the actual UPDATE row count."""
    pool = _FakePool(
        bars=_bars_around_split(before_close="500", after_close="125"),
        n_to_update=120,
    )
    await apply_splits.apply_split(
        pool, "AAPL", date(2020, 8, 31), Decimal("4"),
    )
    # Find the UPDATE-applied call's bind args.
    applied = next(
        c for c in pool.conn.calls
        if c[0] == "execute"
        and "UPDATE platform.split_pre_image_log" in c[1]
    )
    # args: (pre_image_id, n_rows_actually_updated)
    assert applied[2][1] == 120


# ─────────────────────────────────────────────────────────────────────
# Reject paths — implausible ratio + too-many-rows
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reject_implausible_ratio_no_db_writes() -> None:
    """Ratio > RATIO_PLAUSIBILITY_MAX rejects eagerly: no fetch, no
    UPDATE, no pre-image row."""
    pool = _FakePool(bars=[], n_to_update=0)
    result = await apply_splits.apply_split(
        pool, "AAPL", date(2020, 8, 31),
        Decimal("1168"),  # the MCHB mis-encode pattern
    )
    assert result["applied"] is False
    assert result["reason"] == "ratio_implausible"
    # No conn.* call should have fired.
    assert pool.conn.calls == []


@pytest.mark.asyncio
async def test_reject_too_many_rows_no_update_no_pre_image_row() -> None:
    """Aborts before pre-image INSERT and before UPDATE."""
    pool = _FakePool(
        bars=_bars_around_split(before_close="500", after_close="125"),
        n_to_update=apply_splits.MAX_AFFECTED_ROWS_ABSOLUTE + 1,
    )
    result = await apply_splits.apply_split(
        pool, "AAPL", date(2020, 8, 31), Decimal("4"),
    )
    assert result["applied"] is False
    assert result["reason"] == "too_many_rows"
    assert result["n_rows_to_update"] == apply_splits.MAX_AFFECTED_ROWS_ABSOLUTE + 1
    # Pre-image INSERT must NOT have fired.
    assert not any(
        "INSERT INTO platform.split_pre_image_log" in c[1]
        for c in pool.conn.calls
    )
    # UPDATE must NOT have fired.
    assert not any(
        "UPDATE platform.prices_daily" in c[1] and "SET" in c[1]
        for c in pool.conn.calls
    )


# ─────────────────────────────────────────────────────────────────────
# Skip paths — keep parity with the legacy missing_bars + already_adjusted
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skip_missing_bars_no_pre_image() -> None:
    pool = _FakePool(bars=[], n_to_update=0)  # zero rows around the split
    result = await apply_splits.apply_split(
        pool, "AAPL", date(2020, 8, 31), Decimal("4"),
    )
    assert result["applied"] is False
    assert result["reason"] == "missing_bars"
    assert not any(
        "INSERT INTO platform.split_pre_image_log" in c[1]
        for c in pool.conn.calls
    )


@pytest.mark.asyncio
async def test_skip_already_adjusted_no_pre_image() -> None:
    """Adjusted-already (ratio ≈ 1) skips the UPDATE + pre-image row."""
    pool = _FakePool(
        # Pre-split bars are ~$125 either side: the table is already
        # adjusted, observed_ratio ≈ 1.0 < RATIO_RAW_THRESHOLD.
        bars=_bars_around_split(before_close="125", after_close="125"),
        n_to_update=100,
    )
    result = await apply_splits.apply_split(
        pool, "AAPL", date(2020, 8, 31), Decimal("4"),
    )
    assert result["applied"] is False
    assert result["reason"] == "already_adjusted"
    assert not any(
        "INSERT INTO platform.split_pre_image_log" in c[1]
        for c in pool.conn.calls
    )
