"""Tests for `check_delistings` per spec §3.1."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from tpcore.quality.validation.checks.delistings import check_delistings
from tpcore.quality.validation.sources.delistings import (
    DelistingEvent,
    DelistingsSource,
)

from .conftest import FakePool, make_bar


class _StaticDelistingsSource(DelistingsSource):
    def __init__(self, events: list[DelistingEvent]) -> None:
        self._events = events

    def list_delistings(self) -> list[DelistingEvent]:
        return list(self._events)


# ────────────────────────────────────────────────────────────────────────────
# Happy path
# ────────────────────────────────────────────────────────────────────────────


async def test_delisting_passes_when_all_conditions_met() -> None:
    src = _StaticDelistingsSource(
        [
            DelistingEvent(
                ticker="BBBYQ",
                alt_tickers=["BBBY"],
                delisting_date=date(2023, 4, 23),
                reason="bankruptcy",
            )
        ]
    )
    rows = [
        make_bar(
            "BBBYQ",
            date(2023, 4, 21),
            Decimal("0.30"),
            delisted=True,
            delisting_date=date(2023, 4, 23),
        )
    ]
    pool = FakePool(rows)
    result = await check_delistings(pool, src)
    assert result.passed is True
    assert result.failed == 0
    assert result.name == "delistings"


# ────────────────────────────────────────────────────────────────────────────
# Alt-ticker fallback
# ────────────────────────────────────────────────────────────────────────────


async def test_delisting_passes_when_only_alt_ticker_present() -> None:
    """Primary ticker missing, alt_ticker satisfies all conditions → pass."""
    src = _StaticDelistingsSource(
        [
            DelistingEvent(
                ticker="SIVBQ",
                alt_tickers=["SIVB"],
                delisting_date=date(2023, 3, 17),
                reason="bankruptcy",
            )
        ]
    )
    rows = [
        make_bar(
            "SIVB",
            date(2023, 3, 16),
            Decimal("106.04"),
            delisted=True,
            delisting_date=date(2023, 3, 17),
        )
    ]
    pool = FakePool(rows)
    result = await check_delistings(pool, src)
    assert result.passed is True


# ────────────────────────────────────────────────────────────────────────────
# Failure modes
# ────────────────────────────────────────────────────────────────────────────


async def test_delisting_fails_when_ticker_missing() -> None:
    src = _StaticDelistingsSource(
        [DelistingEvent(ticker="ZZZZ", delisting_date=date(2024, 1, 30), reason="acquired")]
    )
    pool = FakePool([])
    result = await check_delistings(pool, src)
    assert result.passed is False
    assert result.failures[0].ticker == "ZZZZ"
    assert result.failures[0].reason == "missing"


async def test_delisting_fails_when_not_marked_delisted() -> None:
    src = _StaticDelistingsSource(
        [DelistingEvent(ticker="BBBYQ", delisting_date=date(2023, 4, 23), reason="bankruptcy")]
    )
    rows = [
        make_bar("BBBYQ", date(2023, 4, 21), Decimal("0.30"), delisted=False, delisting_date=None)
    ]
    pool = FakePool(rows)
    result = await check_delistings(pool, src)
    assert result.passed is False
    assert result.failures[0].reason == "not_delisted"


async def test_delisting_fails_when_delisting_date_null() -> None:
    src = _StaticDelistingsSource(
        [DelistingEvent(ticker="BBBYQ", delisting_date=date(2023, 4, 23), reason="bankruptcy")]
    )
    rows = [
        make_bar("BBBYQ", date(2023, 4, 21), Decimal("0.30"), delisted=True, delisting_date=None)
    ]
    pool = FakePool(rows)
    result = await check_delistings(pool, src)
    assert result.passed is False
    assert result.failures[0].reason == "delisting_date_null"


async def test_delisting_fails_when_date_drift_too_large() -> None:
    """Recorded date 2023-04-23, observed 2023-06-01 → ~28 trading days drift."""
    src = _StaticDelistingsSource(
        [DelistingEvent(ticker="BBBYQ", delisting_date=date(2023, 4, 23), reason="bankruptcy")]
    )
    rows = [
        make_bar(
            "BBBYQ",
            date(2023, 6, 1),
            Decimal("0.30"),
            delisted=True,
            delisting_date=date(2023, 6, 1),
        )
    ]
    pool = FakePool(rows)
    result = await check_delistings(pool, src)
    assert result.passed is False
    assert result.failures[0].reason == "date_drift"


async def test_delisting_fails_when_last_bar_too_early() -> None:
    """Bars stop > 5 trading days before the recorded delisting date."""
    src = _StaticDelistingsSource(
        [DelistingEvent(ticker="BBBYQ", delisting_date=date(2023, 4, 23), reason="bankruptcy")]
    )
    rows = [
        make_bar(
            "BBBYQ",
            date(2023, 3, 1),  # ~37 trading days earlier
            Decimal("0.30"),
            delisted=True,
            delisting_date=date(2023, 4, 23),
        )
    ]
    pool = FakePool(rows)
    result = await check_delistings(pool, src)
    assert result.passed is False
    assert result.failures[0].reason == "last_bar_stale"
