"""End-to-end test: run_suite with the actual `Fixture*Source` classes.

Exercises the YAML loading path on top of the synthetic DB. A separate
file from `test_suite.py` (which uses inline static sources) so the
fixture-load codepath is independently covered.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from tpcore.quality.validation.sources.constituents import FixtureConstituentSource
from tpcore.quality.validation.sources.delistings import FixtureDelistingsSource
from tpcore.quality.validation.sources.splits import FixtureSplitsSource
from tpcore.quality.validation.suite import run_suite

from .conftest import FakePool, make_bar


def _today() -> date:
    return datetime.now(UTC).date()


class _RecordingWriter:
    def __init__(self) -> None:
        self.scores: list = []

    async def write(self, score) -> bool:
        self.scores.append(score)
        return True


def _build_pool_satisfying(write_yaml) -> tuple[FakePool, FixtureDelistingsSource, FixtureConstituentSource, FixtureSplitsSource]:
    """Synthetic DB whose rows satisfy the four checks we write inline."""
    delistings_path = write_yaml(
        "delistings.yaml",
        """
- ticker: SIVBQ
  alt_tickers: [SIVB]
  delisting_date: 2023-03-17
  reason: bankruptcy
""",
    )
    constituents_path = write_yaml(
        "constituents.yaml",
        """
current_sp500_snapshot_date: 2026-05-10
current_sp500:
  - AAPL
recent_removals:
  - ticker: SIVBQ
    removed_date: 2023-03-15
    reason: bankruptcy
    expect_delisted: true
""",
    )
    splits_path = write_yaml(
        "splits.yaml",
        """
- ticker: AAPL
  split_date: 2020-08-31
  ratio: "4:1"
""",
    )
    rows = [
        # SIVBQ delisted
        make_bar(
            "SIVBQ",
            date(2023, 3, 16),
            Decimal("106.04"),
            delisted=True,
            delisting_date=date(2023, 3, 17),
        ),
        # AAPL recent + split bars
        make_bar("AAPL", _today() - timedelta(days=1), Decimal("180.00")),
        make_bar("AAPL", date(2020, 8, 28), Decimal("125.00")),
        make_bar("AAPL", date(2020, 8, 31), Decimal("125.00")),
    ]
    return (
        FakePool(rows),
        FixtureDelistingsSource(path=delistings_path),
        FixtureConstituentSource(path=constituents_path),
        FixtureSplitsSource(path=splits_path),
    )


async def test_e2e_passes_with_satisfying_synthetic_data(write_yaml) -> None:
    pool, de, co, sp = _build_pool_satisfying(write_yaml)
    writer = _RecordingWriter()
    result = await run_suite(pool, delistings=de, constituents=co, splits=sp, writer=writer)
    assert result.passed is True
    assert len(writer.scores) == 15  # +insider_sentiment_freshness (2026-05-16)
    sources = {s.source for s in writer.scores}
    assert sources == {
        "validation.delistings",
        "validation.constituent",
        "validation.splits",
        "validation.row_integrity",
        "validation.fundamentals_integrity",
        "validation.corporate_actions_integrity",
        "validation.catalyst_events_freshness",
        "validation.sec_filings_freshness",
        "validation.liquidity_tiers_freshness",
        "validation.ticker_classifications_coverage",
        "validation.macro_indicators_freshness",
        "validation.prices_daily_freshness",
        "validation.prices_daily_completeness",
        "validation.options_max_pain_freshness",
        "validation.insider_sentiment_freshness",
    }


async def test_e2e_fails_after_mutating_a_split_bar(write_yaml) -> None:
    """Break the split adjustment, re-run, assert exactly the splits check fails."""
    pool, de, co, sp = _build_pool_satisfying(write_yaml)
    # Replace AAPL 2020-08-31 close with the raw (unadjusted) price → ratio ≈ 4.0.
    for r in pool.rows:
        if r["ticker"] == "AAPL" and r["date"] == date(2020, 8, 28):
            r["close"] = Decimal("500.00")  # pre-split raw

    writer = _RecordingWriter()
    result = await run_suite(pool, delistings=de, constituents=co, splits=sp, writer=writer)
    assert result.passed is False
    failed_names = {c.name for c in result.checks if not c.passed}
    assert failed_names == {"splits"}
    splits_score = next(s for s in writer.scores if s.source == "validation.splits")
    assert splits_score.stale is True
