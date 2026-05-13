"""Tests for ``check_corporate_actions_integrity``."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from tpcore.quality.validation.checks.corporate_actions_integrity import (
    check_corporate_actions_integrity,
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
    r = await check_corporate_actions_integrity(pool)
    assert r.passed is True


async def test_fails_on_implausible_ratio():
    rows = [{
        "ticker": "MCHB", "action_date": date(2021, 7, 30),
        "action_type": "dividend", "ratio": Decimal("2569"),
        "violation": "ratio_implausible",
    }]
    pool = _FakePool(1, rows)
    r = await check_corporate_actions_integrity(pool)
    assert r.passed is False
    assert r.failures[0].reason == "ratio_implausible"


async def test_fails_on_zero_ratio():
    rows = [{
        "ticker": "XX", "action_date": date(2024, 1, 1),
        "action_type": "split", "ratio": Decimal("0"),
        "violation": "ratio_nonpositive",
    }]
    pool = _FakePool(1, rows)
    r = await check_corporate_actions_integrity(pool)
    assert r.passed is False
    assert r.failures[0].reason == "ratio_nonpositive"


async def test_signature_accepts_source():
    pool = _FakePool(0, [])
    r = await check_corporate_actions_integrity(pool, source=None)
    assert r.passed is True
