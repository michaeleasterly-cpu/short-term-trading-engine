"""Tests for `check_constituent_snapshot` per spec §3.2."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from tpcore.quality.validation.checks.constituent import check_constituent_snapshot
from tpcore.quality.validation.sources.constituents import (
    ConstituentSource,
    RemovalEvent,
)

from .conftest import FakePool, make_bar


class _StaticConstituentSource(ConstituentSource):
    def __init__(self, current: list[str], removals: list[RemovalEvent]) -> None:
        self._current = current
        self._removals = removals

    def list_current_sp500(self) -> list[str]:
        return list(self._current)

    def list_recent_removals(self) -> list[RemovalEvent]:
        return list(self._removals)


def _today() -> date:
    return datetime.now(UTC).date()


def _recent_bar(ticker: str, days_ago: int = 1) -> dict:
    return make_bar(ticker, _today() - timedelta(days=days_ago), Decimal("100.00"))


# ────────────────────────────────────────────────────────────────────────────
# Happy path
# ────────────────────────────────────────────────────────────────────────────


async def test_constituent_passes_when_all_conditions_met() -> None:
    src = _StaticConstituentSource(
        current=["AAPL", "MSFT"],
        removals=[
            RemovalEvent(
                ticker="SIVBQ",
                removed_date=date(2023, 3, 15),
                reason="bankruptcy",
                expect_delisted=True,
            )
        ],
    )
    rows = [
        _recent_bar("AAPL"),
        _recent_bar("MSFT"),
        make_bar(
            "SIVBQ",
            date(2023, 3, 16),
            Decimal("106.04"),
            delisted=True,
            delisting_date=date(2023, 3, 17),
        ),
    ]
    pool = FakePool(rows)
    result = await check_constituent_snapshot(pool, src)
    assert result.passed is True
    assert result.failed == 0
    assert result.name == "constituent"


# ────────────────────────────────────────────────────────────────────────────
# Failure modes
# ────────────────────────────────────────────────────────────────────────────


async def test_constituent_fails_when_current_ticker_missing() -> None:
    src = _StaticConstituentSource(
        current=["AAPL", "MSFT"],
        removals=[
            RemovalEvent(
                ticker="SIVBQ",
                removed_date=date(2023, 3, 15),
                reason="bankruptcy",
                expect_delisted=True,
            )
        ],
    )
    rows = [
        _recent_bar("AAPL"),
        # MSFT missing entirely
        make_bar(
            "SIVBQ",
            date(2023, 3, 16),
            Decimal("106.04"),
            delisted=True,
            delisting_date=date(2023, 3, 17),
        ),
    ]
    pool = FakePool(rows)
    result = await check_constituent_snapshot(pool, src)
    assert result.passed is False
    assert any(f.ticker == "MSFT" and f.reason == "missing" for f in result.failures)


async def test_constituent_fails_when_current_ticker_stale() -> None:
    """Active S&P name with bars but none within last 5 trading days."""
    src = _StaticConstituentSource(
        current=["AAPL"],
        removals=[
            RemovalEvent(
                ticker="SIVBQ",
                removed_date=date(2023, 3, 15),
                reason="bankruptcy",
                expect_delisted=True,
            )
        ],
    )
    rows = [
        make_bar("AAPL", _today() - timedelta(days=30), Decimal("100.00")),  # stale
        make_bar(
            "SIVBQ",
            date(2023, 3, 16),
            Decimal("106.04"),
            delisted=True,
            delisting_date=date(2023, 3, 17),
        ),
    ]
    pool = FakePool(rows)
    result = await check_constituent_snapshot(pool, src)
    assert result.passed is False
    assert any(f.ticker == "AAPL" and f.reason == "stale" for f in result.failures)


async def test_constituent_fails_when_removal_not_marked_delisted() -> None:
    src = _StaticConstituentSource(
        current=["AAPL"],
        removals=[
            RemovalEvent(
                ticker="SIVBQ",
                removed_date=date(2023, 3, 15),
                reason="bankruptcy",
                expect_delisted=True,
            )
        ],
    )
    rows = [
        _recent_bar("AAPL"),
        # SIVBQ present but not delisted
        make_bar("SIVBQ", date(2023, 3, 16), Decimal("106.04"), delisted=False),
    ]
    pool = FakePool(rows)
    result = await check_constituent_snapshot(pool, src)
    assert result.passed is False
    assert any(f.ticker == "SIVBQ" and f.reason == "not_delisted" for f in result.failures)
