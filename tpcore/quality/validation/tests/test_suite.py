"""Tests for `run_suite` (the orchestrator)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from tpcore.quality.data_quality import DataQualityScore, DataQualityWriter
from tpcore.quality.validation.sources.constituents import (
    ConstituentSource,
    RemovalEvent,
)
from tpcore.quality.validation.sources.delistings import (
    DelistingEvent,
    DelistingsSource,
)
from tpcore.quality.validation.sources.splits import SplitEvent, SplitsSource
from tpcore.quality.validation.suite import run_suite

from .conftest import FakePool, make_bar

# ────────────────────────────────────────────────────────────────────────────
# Inline source stubs
# ────────────────────────────────────────────────────────────────────────────


class _StaticSplits(SplitsSource):
    def __init__(self, events: list[SplitEvent]) -> None:
        self._e = events

    def list_splits(self) -> list[SplitEvent]:
        return list(self._e)


class _StaticDelistings(DelistingsSource):
    def __init__(self, events: list[DelistingEvent]) -> None:
        self._e = events

    def list_delistings(self) -> list[DelistingEvent]:
        return list(self._e)


class _StaticConstituents(ConstituentSource):
    def __init__(self, current: list[str], removals: list[RemovalEvent]) -> None:
        self._c = current
        self._r = removals

    def list_current_sp500(self) -> list[str]:
        return list(self._c)

    def list_recent_removals(self) -> list[RemovalEvent]:
        return list(self._r)


class _RecordingWriter(DataQualityWriter):
    """Records every score handed to `write` instead of hitting the DB."""

    def __init__(self) -> None:  # type: ignore[no-untyped-def]
        self.scores: list[DataQualityScore] = []

    async def write(self, score: DataQualityScore) -> bool:  # type: ignore[override]
        self.scores.append(score)
        return True


# ────────────────────────────────────────────────────────────────────────────
# Builders
# ────────────────────────────────────────────────────────────────────────────


def _today() -> date:
    return datetime.now(UTC).date()


def _all_passing_setup() -> tuple[FakePool, _StaticDelistings, _StaticConstituents, _StaticSplits]:
    delistings = _StaticDelistings(
        [
            DelistingEvent(
                ticker="SIVBQ",
                alt_tickers=["SIVB"],
                delisting_date=date(2023, 3, 17),
                reason="bankruptcy",
            )
        ]
    )
    constituents = _StaticConstituents(
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
    splits = _StaticSplits(
        [SplitEvent(ticker="AAPL", split_date=date(2020, 8, 31), ratio_num=4, ratio_den=1)]
    )
    rows = [
        # Delistings + removal coverage
        make_bar(
            "SIVBQ",
            date(2023, 3, 16),
            Decimal("106.04"),
            delisted=True,
            delisting_date=date(2023, 3, 17),
        ),
        # Recent S&P bar
        make_bar("AAPL", _today() - timedelta(days=1), Decimal("100.00")),
        # Splits ratio ≈ 1.0
        make_bar("AAPL", date(2020, 8, 28), Decimal("125.00")),
        make_bar("AAPL", date(2020, 8, 31), Decimal("125.00")),
    ]
    return FakePool(rows), delistings, constituents, splits


# ────────────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────────────


async def test_run_suite_passes_when_all_checks_pass() -> None:
    pool, delistings, constituents, splits = _all_passing_setup()
    writer = _RecordingWriter()
    result = await run_suite(
        pool, delistings=delistings, constituents=constituents, splits=splits, writer=writer
    )
    assert result.passed is True
    assert {c.name for c in result.checks} == {
        "delistings", "constituent", "splits", "row_integrity",
        "fundamentals_integrity", "fundamentals_quarterly_completeness",
        "corporate_actions_integrity", "corporate_actions_completeness",
        "earnings_events_freshness", "earnings_events_monotone",
        "sec_filings_freshness",
        "sec_insider_monotone",
        "liquidity_tiers_freshness", "liquidity_tiers_completeness",
        "ticker_classifications_coverage",
        "macro_indicators_freshness", "macro_indicators_completeness",
        "prices_daily_freshness", "prices_daily_completeness",
        "options_max_pain_freshness",
        "insider_sentiment_freshness", "social_sentiment_freshness",
        "fear_greed_freshness", "short_interest_freshness",
        "borrow_rates_freshness", "aaii_sentiment_freshness",
    }
    assert all(c.passed for c in result.checks)


async def test_run_suite_writes_one_score_per_check() -> None:
    pool, delistings, constituents, splits = _all_passing_setup()
    writer = _RecordingWriter()
    await run_suite(
        pool, delistings=delistings, constituents=constituents, splits=splits, writer=writer
    )
    assert len(writer.scores) == 26  # +liquidity_tiers_completeness (2026-05-20)
    sources = {s.source for s in writer.scores}
    assert sources == {
        "validation.delistings",
        "validation.constituent",
        "validation.splits",
        "validation.row_integrity",
        "validation.fundamentals_integrity",
        "validation.fundamentals_quarterly_completeness",
        "validation.corporate_actions_completeness",
        "validation.corporate_actions_integrity",
        "validation.earnings_events_freshness",
        "validation.earnings_events_monotone",
        "validation.sec_filings_freshness",
        "validation.sec_insider_monotone",
        "validation.liquidity_tiers_freshness",
        "validation.liquidity_tiers_completeness",
        "validation.ticker_classifications_coverage",
        "validation.macro_indicators_freshness",
        "validation.macro_indicators_completeness",
        "validation.prices_daily_freshness",
        "validation.prices_daily_completeness",
        "validation.options_max_pain_freshness",
        "validation.insider_sentiment_freshness",
        "validation.social_sentiment_freshness",
        "validation.fear_greed_freshness",
        "validation.short_interest_freshness",
        "validation.borrow_rates_freshness",
        "validation.aaii_sentiment_freshness",
    }


async def test_run_suite_score_field_mapping() -> None:
    pool, delistings, constituents, splits = _all_passing_setup()
    writer = _RecordingWriter()
    result = await run_suite(
        pool, delistings=delistings, constituents=constituents, splits=splits, writer=writer
    )
    by_source = {s.source: s for s in writer.scores}
    splits_score = by_source["validation.splits"]
    splits_check = next(c for c in result.checks if c.name == "splits")
    assert splits_score.timestamp == result.started_at
    assert splits_score.latency_ms == splits_check.duration_ms
    assert splits_score.missing_bars == splits_check.failed
    assert splits_score.stale is (not splits_check.passed)
    assert splits_score.confidence == Decimal("1.000")
    # Notes is JSON-serialized failures
    assert splits_score.notes is not None
    assert splits_score.notes.startswith("[")  # JSON list


async def test_run_suite_aggregates_failures() -> None:
    """One failing check → suite.passed=False but other check rows still recorded."""
    pool, _, constituents, splits = _all_passing_setup()
    # Delisting that won't be found
    delistings = _StaticDelistings(
        [DelistingEvent(ticker="ZZZZ", delisting_date=date(2024, 1, 1), reason="acquired")]
    )
    writer = _RecordingWriter()
    result = await run_suite(
        pool, delistings=delistings, constituents=constituents, splits=splits, writer=writer
    )
    assert result.passed is False
    failed_checks = [c for c in result.checks if not c.passed]
    assert len(failed_checks) == 1
    assert failed_checks[0].name == "delistings"
    # All 26 rows still written (+liquidity_tiers_completeness 2026-05-20)
    assert len(writer.scores) == 26


async def test_run_suite_wraps_check_exception() -> None:
    """An exception in a single check produces a failed CheckResult, others run."""
    pool, delistings, constituents, splits = _all_passing_setup()

    class _BoomSplits(SplitsSource):
        def list_splits(self) -> list[SplitEvent]:
            raise RuntimeError("source exploded")

    writer = _RecordingWriter()
    result = await run_suite(
        pool,
        delistings=delistings,
        constituents=constituents,
        splits=_BoomSplits(),
        writer=writer,
    )
    assert result.passed is False
    splits_check = next(c for c in result.checks if c.name == "splits")
    assert splits_check.passed is False
    assert splits_check.failures[0].reason == "exception"
    # Other checks still pass
    delistings_check = next(c for c in result.checks if c.name == "delistings")
    assert delistings_check.passed is True
