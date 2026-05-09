"""Tests for `check_splits` per spec §3.3."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from tpcore.quality.validation.checks.splits import check_splits
from tpcore.quality.validation.sources.splits import SplitEvent, SplitsSource

from .conftest import FakePool, make_bar


class _StaticSplitsSource(SplitsSource):
    def __init__(self, events: list[SplitEvent]) -> None:
        self._events = events

    def list_splits(self) -> list[SplitEvent]:
        return list(self._events)


# ────────────────────────────────────────────────────────────────────────────
# Happy path
# ────────────────────────────────────────────────────────────────────────────


async def test_splits_passes_when_close_is_adjusted() -> None:
    """A correctly-adjusted feed shows ratio ~ 1.0 across the split day."""
    src = _StaticSplitsSource([SplitEvent(ticker="AAPL", split_date=date(2020, 8, 31), ratio_num=4, ratio_den=1)])
    rows = [
        make_bar("AAPL", date(2020, 8, 28), Decimal("125.00")),  # last trading day before split
        make_bar("AAPL", date(2020, 8, 31), Decimal("125.50")),  # ratio = 125 / 125.50 ≈ 0.996 — in band
    ]
    pool = FakePool(rows)
    result = await check_splits(pool, src)
    assert result.name == "splits"
    assert result.passed is True
    assert result.failed == 0
    assert result.failures == []


# ────────────────────────────────────────────────────────────────────────────
# Failure modes
# ────────────────────────────────────────────────────────────────────────────


async def test_splits_fails_on_unadjusted_4_to_1_ratio() -> None:
    """Raw close → ratio ≈ 0.25 for a 4:1 split, far outside [0.99, 1.01]."""
    src = _StaticSplitsSource([SplitEvent(ticker="AAPL", split_date=date(2020, 8, 31), ratio_num=4, ratio_den=1)])
    rows = [
        make_bar("AAPL", date(2020, 8, 28), Decimal("500.00")),  # pre-split price
        make_bar("AAPL", date(2020, 8, 31), Decimal("125.00")),  # post-split (raw) — ratio 4.0
    ]
    pool = FakePool(rows)
    result = await check_splits(pool, src)
    assert result.passed is False
    assert result.failed == 1
    assert result.failures[0].ticker == "AAPL"
    assert result.failures[0].reason == "ratio_off"


async def test_splits_fails_on_unadjusted_10_to_1_ratio() -> None:
    src = _StaticSplitsSource([SplitEvent(ticker="NVDA", split_date=date(2024, 6, 10), ratio_num=10, ratio_den=1)])
    rows = [
        make_bar("NVDA", date(2024, 6, 7), Decimal("1200.00")),
        make_bar("NVDA", date(2024, 6, 10), Decimal("120.00")),  # ratio 10.0
    ]
    pool = FakePool(rows)
    result = await check_splits(pool, src)
    assert result.passed is False
    assert result.failures[0].reason == "ratio_off"


async def test_splits_fails_when_pre_split_bar_missing() -> None:
    src = _StaticSplitsSource([SplitEvent(ticker="AAPL", split_date=date(2020, 8, 31), ratio_num=4, ratio_den=1)])
    rows = [
        make_bar("AAPL", date(2020, 8, 31), Decimal("125.00")),  # only post-split bar
    ]
    pool = FakePool(rows)
    result = await check_splits(pool, src)
    assert result.passed is False
    assert result.failures[0].reason == "missing"


async def test_splits_aggregates_multiple_events() -> None:
    src = _StaticSplitsSource(
        [
            SplitEvent(ticker="AAPL", split_date=date(2020, 8, 31), ratio_num=4, ratio_den=1),
            SplitEvent(ticker="NVDA", split_date=date(2024, 6, 10), ratio_num=10, ratio_den=1),
        ]
    )
    rows = [
        make_bar("AAPL", date(2020, 8, 28), Decimal("125.00")),
        make_bar("AAPL", date(2020, 8, 31), Decimal("125.00")),
        # NVDA only post-split → fail
        make_bar("NVDA", date(2024, 6, 10), Decimal("120.00")),
    ]
    pool = FakePool(rows)
    result = await check_splits(pool, src)
    assert result.total == 2
    assert result.failed == 1
    assert result.passed is False
