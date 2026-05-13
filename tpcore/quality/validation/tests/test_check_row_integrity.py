"""Tests for ``check_row_integrity``."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from tpcore.quality.validation.checks.row_integrity import (
    FAILURE_CAP,
    check_row_integrity,
)


class _FakeConn:
    """Returns a fixed COUNT and a fixed row list per integrity test.

    The check issues two queries — one COUNT(*) and one full SELECT —
    against the SAME predicate. The fake routes them by SQL substring.
    """

    def __init__(self, count: int, rows: list[dict]) -> None:
        self._count = count
        self._rows = rows

    async def fetchval(self, sql: str) -> int:
        assert "COUNT(*)" in sql
        return self._count

    async def fetch(self, sql: str, limit: int) -> list[dict]:
        return self._rows[:limit]


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(self, count: int, rows: list[dict]) -> None:
        self.conn = _FakeConn(count, rows)

    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(self.conn)


async def test_row_integrity_passes_when_no_violations():
    pool = _FakePool(count=0, rows=[])
    result = await check_row_integrity(pool)
    assert result.passed is True
    assert result.total == 1
    assert result.failed == 0
    assert result.failures == []


async def test_row_integrity_fails_on_zero_close():
    rows = [
        {
            "ticker": "COCO",
            "date": date(2014, 12, 5),
            "close": Decimal("0.000000"),
            "high": Decimal("0.000000"),
            "low": Decimal("0.000000"),
            "volume": 0,
            "violation": "close_nonpositive",
        }
    ]
    pool = _FakePool(count=1, rows=rows)
    result = await check_row_integrity(pool)
    assert result.passed is False
    assert result.failed == 1
    assert len(result.failures) == 1
    f = result.failures[0]
    assert f.ticker.startswith("COCO@")
    assert f.reason == "close_nonpositive"
    assert "close=0.000000" in (f.observed or "")


async def test_row_integrity_caps_failure_list():
    rows = [
        {
            "ticker": f"T{i:04d}",
            "date": date(2020, 1, 1),
            "close": Decimal("0"),
            "high": Decimal("0"),
            "low": Decimal("0"),
            "volume": 0,
            "violation": "close_nonpositive",
        }
        for i in range(FAILURE_CAP * 3)
    ]
    pool = _FakePool(count=FAILURE_CAP * 3, rows=rows)
    result = await check_row_integrity(pool)
    # Surface the real count via .failed even though the list is capped.
    assert result.failed == 1  # boolean fail/pass — actual count in .total/notes
    # FailureDetail list is bounded by FAILURE_CAP regardless of input size.
    assert len(result.failures) <= FAILURE_CAP


async def test_row_integrity_classifies_high_lt_low():
    rows = [
        {
            "ticker": "WEIRD",
            "date": date(2024, 6, 1),
            "close": Decimal("10"),
            "high": Decimal("9"),  # below low — impossible bar
            "low": Decimal("10"),
            "volume": 1000,
            "violation": "high_lt_low",
        }
    ]
    pool = _FakePool(count=1, rows=rows)
    result = await check_row_integrity(pool)
    assert result.passed is False
    assert result.failures[0].reason == "high_lt_low"


async def test_row_integrity_classifies_future_date():
    rows = [
        {
            "ticker": "AAPL",
            "date": date(2099, 1, 1),
            "close": Decimal("200"),
            "high": Decimal("201"),
            "low": Decimal("199"),
            "volume": 1_000_000,
            "violation": "future_date",
        }
    ]
    pool = _FakePool(count=1, rows=rows)
    result = await check_row_integrity(pool)
    assert result.failures[0].reason == "future_date"


async def test_row_integrity_signature_accepts_source_arg():
    # The suite passes ``source=None`` for parity with the other checks;
    # the function must accept and ignore it.
    pool = _FakePool(count=0, rows=[])
    result = await check_row_integrity(pool, source=None)
    assert result.passed is True
