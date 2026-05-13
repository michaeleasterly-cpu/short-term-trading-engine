"""Tests for ``check_fundamentals_integrity``."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from tpcore.quality.validation.checks.fundamentals_integrity import (
    check_fundamentals_integrity,
)


class _FakeConn:
    def __init__(self, count: int, rows: list[dict]) -> None:
        self._count = count
        self._rows = rows

    async def fetchval(self, sql: str) -> int:
        return self._count

    async def fetch(self, sql: str, limit: int) -> list[dict]:
        return self._rows[:limit]


class _FakeCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(self, count: int, rows: list[dict]) -> None:
        self.conn = _FakeConn(count, rows)

    def acquire(self) -> _FakeCM:
        return _FakeCM(self.conn)


async def test_passes_when_no_violations():
    pool = _FakePool(0, [])
    r = await check_fundamentals_integrity(pool)
    assert r.passed is True
    assert r.failed == 0


async def test_fails_on_period_after_filing():
    rows = [{
        "ticker": "BHP", "period_end_date": date(2007, 12, 31),
        "filing_date": date(2007, 12, 30), "shares_outstanding": Decimal("1"),
        "violation": "period_after_filing",
    }]
    pool = _FakePool(1, rows)
    r = await check_fundamentals_integrity(pool)
    assert r.passed is False
    assert r.failures[0].reason == "period_after_filing"


async def test_fails_on_shares_zero():
    rows = [{
        "ticker": "DMB", "period_end_date": date(2014, 2, 28),
        "filing_date": date(2014, 4, 1), "shares_outstanding": Decimal("0"),
        "violation": "shares_nonpositive",
    }]
    pool = _FakePool(1, rows)
    r = await check_fundamentals_integrity(pool)
    assert r.passed is False
    assert r.failures[0].reason == "shares_nonpositive"


async def test_signature_accepts_source():
    pool = _FakePool(0, [])
    r = await check_fundamentals_integrity(pool, source=None)
    assert r.passed is True
