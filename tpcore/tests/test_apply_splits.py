"""Tests for `tpcore.data.apply_splits` against an in-memory fake DB."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tpcore.data.apply_splits import (
    RATIO_RAW_THRESHOLD,
    apply_split,
)


# ────────────────────────────────────────────────────────────────────────────
# Fake pool that holds prices_daily rows in a dict and routes SELECT / UPDATE
# ────────────────────────────────────────────────────────────────────────────


class _FakeConn:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows  # mutable; UPDATE modifies in place
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args) -> list[dict]:
        self.calls.append((sql, args))
        sql_lower = sql.lower()
        if "from platform.prices_daily" in sql_lower and "ticker = $1" in sql_lower:
            ticker = args[0]
            on_or_before = args[1]
            matched = [r for r in self.rows if r["ticker"] == ticker and r["date"] <= on_or_before]
            matched.sort(key=lambda r: r["date"], reverse=True)
            return matched[:2]
        return []

    async def execute(self, sql: str, *args) -> str:
        self.calls.append((sql, args))
        sql_lower = sql.strip().lower()
        if sql_lower.startswith("update platform.prices_daily"):
            ratio = Decimal(str(args[0]))
            ticker = args[1]
            action_date = args[2]
            n = 0
            for r in self.rows:
                if r["ticker"] == ticker and r["date"] < action_date:
                    for k in ("open", "high", "low", "close", "adjusted_close"):
                        r[k] = Decimal(str(r[k])) / ratio
                    r["volume"] = int(Decimal(str(r["volume"])) * ratio)
                    n += 1
            return f"UPDATE {n}"
        return ""


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(self, rows: list[dict]) -> None:
        self.conn = _FakeConn(rows)

    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(self.conn)


def _bar(ticker: str, d: date, close: Decimal | str) -> dict:
    c = Decimal(str(close))
    return {
        "ticker": ticker,
        "date": d,
        "open": c, "high": c, "low": c, "close": c, "adjusted_close": c,
        "volume": 1_000_000,
        "delisted": False, "delisting_date": None,
    }


# ────────────────────────────────────────────────────────────────────────────
# Raw vs adjusted heuristic
# ────────────────────────────────────────────────────────────────────────────


async def test_apply_split_adjusts_when_data_is_raw() -> None:
    """AAPL-style: pre-split close ≈ 4× post-split close → raw → apply."""
    rows = [
        _bar("AAPL", date(2020, 8, 28), "484.80"),
        _bar("AAPL", date(2020, 8, 31), "125.35"),
    ]
    pool = _FakePool(rows)
    result = await apply_split(pool, "AAPL", date(2020, 8, 31), Decimal("4"))
    assert result["applied"] is True
    assert result["n_rows_updated"] == 1
    # After adjustment, pre-split close should be 484.80 / 4 = 121.20
    assert rows[0]["close"] == Decimal("121.20")
    assert rows[1]["close"] == Decimal("125.35")  # post-split untouched


async def test_apply_split_skips_when_data_already_adjusted() -> None:
    """NVDA-style: pre-split close already ≈ post-split close → skip."""
    rows = [
        _bar("NVDA", date(2024, 6, 7), "120.77"),
        _bar("NVDA", date(2024, 6, 10), "121.86"),
    ]
    pool = _FakePool(rows)
    result = await apply_split(pool, "NVDA", date(2024, 6, 10), Decimal("10"))
    assert result["applied"] is False
    assert result["reason"] == "already_adjusted"
    # Bars unchanged
    assert rows[0]["close"] == Decimal("120.77")
    assert rows[1]["close"] == Decimal("121.86")


async def test_apply_split_skips_when_bars_missing() -> None:
    pool = _FakePool([])
    result = await apply_split(pool, "ZZZZ", date(2024, 6, 10), Decimal("10"))
    assert result["applied"] is False
    assert result["reason"] == "missing_bars"


# ────────────────────────────────────────────────────────────────────────────
# Idempotency
# ────────────────────────────────────────────────────────────────────────────


async def test_apply_split_is_idempotent() -> None:
    rows = [
        _bar("AAPL", date(2020, 8, 28), "484.80"),
        _bar("AAPL", date(2020, 8, 31), "125.35"),
    ]
    pool = _FakePool(rows)
    r1 = await apply_split(pool, "AAPL", date(2020, 8, 31), Decimal("4"))
    r2 = await apply_split(pool, "AAPL", date(2020, 8, 31), Decimal("4"))
    assert r1["applied"] is True
    assert r2["applied"] is False
    assert r2["reason"] == "already_adjusted"
    assert rows[0]["close"] == Decimal("121.20")  # only adjusted once


async def test_apply_split_threshold_constant_documented() -> None:
    """Sanity: threshold is between 1 (adjusted) and the smallest forward split factor (2)."""
    assert Decimal("1.0") < RATIO_RAW_THRESHOLD < Decimal("2.0")


# ────────────────────────────────────────────────────────────────────────────
# Cumulative splits (NVDA-style: two splits in the same ticker history)
# ────────────────────────────────────────────────────────────────────────────


async def test_apply_two_splits_compounds_correctly_when_both_raw() -> None:
    """NVDA had a 4:1 in 2021 and 10:1 in 2024. If both are raw, applying both
    in any order divides pre-2021 prices by 40 (4 * 10)."""
    rows = [
        _bar("NVDA", date(2020, 1, 2), "120"),       # pre-everything
        _bar("NVDA", date(2021, 7, 19), "800"),       # last day before 2021 split, raw
        _bar("NVDA", date(2021, 7, 20), "200"),       # after 2021 split, raw vs the 4:1
        _bar("NVDA", date(2024, 6, 7), "1200"),       # last day before 2024 split, raw vs the 10:1
        _bar("NVDA", date(2024, 6, 10), "120"),       # after 2024 split
    ]
    pool = _FakePool(rows)
    # Apply 2024 split first
    r1 = await apply_split(pool, "NVDA", date(2024, 6, 10), Decimal("10"))
    assert r1["applied"] is True
    # Apply 2021 split second
    r2 = await apply_split(pool, "NVDA", date(2021, 7, 20), Decimal("4"))
    assert r2["applied"] is True

    # pre-2020 row: divided by 10 then by 4 = 40
    assert rows[0]["close"] == Decimal("3")  # 120 / 40
    # 2021-07-19 row: divided by 10 then by 4 = 40
    assert rows[1]["close"] == Decimal("20")  # 800 / 40
    # 2021-07-20 row: divided by 10 only (not before the 2021 split anymore once we apply the 2021 split, but before the 2024)
    assert rows[2]["close"] == Decimal("20")  # 200 / 10
    # 2024-06-07: divided by 10 only
    assert rows[3]["close"] == Decimal("120")  # 1200 / 10
    # 2024-06-10: untouched
    assert rows[4]["close"] == Decimal("120")
