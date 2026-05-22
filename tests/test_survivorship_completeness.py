"""Sentinel test — survivorship coverage in ``platform.prices_daily``.

The 2026-05-22 corpus audit (PR #281) found 18 of 20 known historical
delistings completely absent from ``platform.prices_daily``,
structurally biasing every backtest credibility score. The
``historical_delisted_universe`` ops stage closes the gap; THIS test
is the proof that the gap stays closed.

For each anchor ticker:

1. A ``prices_daily`` row exists with ``delisted=true``.
2. The ticker has a bar within ±5 trading days of the spec-known
   delisting date.
3. The ticker has ≥ 500 bars total (for tickers that traded ≥ 2 years
   pre-delisting — a regression where only the final bar lands would
   silently re-introduce the bias).

DB-skip-gated for CI: ``streamlit``-style DB integration tests are
opt-in via ``DATABASE_URL`` per the existing
``tpcore/tests/test_ingest_fmp_bars_cross_validation.py`` precedent.
Operator runs this against the live Supabase after the post-merge
one-shot ``--stage historical_delisted_universe`` invocation.
"""
from __future__ import annotations

import os
from datetime import date

import pytest

# The anchor sample — pinned in the operator instructions and the
# tpcore.data.survivorship_backfill.KNOWN_DELISTINGS manifest. Each
# row is (ticker, spec_delisting_date, minimum_bars_expected).
#
# minimum_bars_expected reflects the ticker's actual public-market
# trading life pre-delisting; tickers with shorter histories carry
# proportionally lower thresholds (FB pre-rename, ANSS pending close,
# etc.). 500 is the operator-spec minimum for ≥ 2-year traders.
_ANCHOR_DELISTINGS: list[tuple[str, date, int]] = [
    # 2021
    ("WORK",  date(2021, 7, 21),  500),
    ("ALXN",  date(2021, 7, 21),  1000),
    # 2022
    ("TWTR",  date(2022, 10, 27), 1000),
    ("FB",    date(2022, 6, 9),   1500),
    ("XLNX",  date(2022, 2, 14),  1000),
    ("VIAC",  date(2022, 2, 16),  500),
    ("DISCA", date(2022, 4, 8),   500),
    ("ABMD",  date(2022, 12, 22), 1000),
    ("CERN",  date(2022, 6, 8),   1000),
    # 2023
    ("SIVB",  date(2023, 3, 10),  1000),
    ("SBNY",  date(2023, 3, 12),  500),
    ("FRC",   date(2023, 5, 1),   500),
    ("ATVI",  date(2023, 10, 13), 1000),
    ("VMW",   date(2023, 11, 22), 1000),
    ("FISV",  date(2023, 7, 21),  1000),
    # 2024
    ("SPLK",  date(2024, 3, 18),  500),
    ("ANSS",  date(2024, 9, 25),  1000),
]

# ±5 trading-day tolerance: the actual delisting date depends on the
# exchange's mechanical close-out, which doesn't always equal the
# operator's spec-known event date (FMP's truncation point can be
# ~3 trading days earlier or later). The original validation-fixture
# precedent (tpcore/quality/validation/fixtures/delistings.yaml) uses
# the same tolerance band.
_DELISTING_DATE_TOLERANCE_DAYS = 7  # ±5 trading days ≈ 7 calendar days


def _have_database_url() -> bool:
    return bool(os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_IPV4"))


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    not _have_database_url(),
    reason="sentinel test requires DATABASE_URL[_IPV4]; CI skip is expected",
)
@pytest.mark.parametrize("ticker, spec_date, min_bars", _ANCHOR_DELISTINGS)
async def test_anchor_delisting_present_with_delisted_marker(
    ticker: str, spec_date: date, min_bars: int,
) -> None:
    """Each anchor delisted ticker has a prices_daily row with
    delisted=true, a bar near the spec delisting date, and ≥ min_bars
    of trading history. Together these three predicates prove the
    survivorship bias is closed for the sample."""
    import asyncpg

    db_url = os.environ.get("DATABASE_URL") or os.environ["DATABASE_URL_IPV4"]
    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        # Predicate 1: delisted=true row exists for the ticker.
        marker = await conn.fetchrow(
            """
            SELECT COUNT(*) AS row_count,
                   BOOL_OR(delisted) AS any_delisted,
                   MAX(delisting_date) AS max_delisting_date,
                   MAX(date) AS last_bar_date,
                   MIN(date) AS first_bar_date
            FROM platform.prices_daily
            WHERE ticker = $1
            """,
            ticker,
        )
        if marker is None or (marker["row_count"] or 0) == 0:
            pytest.fail(
                f"{ticker}: no rows in platform.prices_daily — survivorship gap not closed; "
                f"operator must run ``.venv/bin/python scripts/ops.py "
                f"--stage historical_delisted_universe``"
            )
        assert marker["any_delisted"] is True, (
            f"{ticker}: present in prices_daily but delisted=false on every row — "
            f"survivorship marker not applied"
        )

        # Predicate 2: bar near the spec delisting date.
        delta_days = abs((marker["last_bar_date"] - spec_date).days)
        assert delta_days <= _DELISTING_DATE_TOLERANCE_DAYS, (
            f"{ticker}: last bar {marker['last_bar_date']} is "
            f"{delta_days} days from spec date {spec_date}; tolerance is "
            f"±{_DELISTING_DATE_TOLERANCE_DAYS} calendar days"
        )

        # Predicate 3: ≥ min_bars of trading history.
        assert (marker["row_count"] or 0) >= min_bars, (
            f"{ticker}: only {marker['row_count']} bars present — "
            f"expected ≥ {min_bars}; the backfill landed only the final "
            f"bar(s), not the full trading life. Run the stage again."
        )
    finally:
        await conn.close()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    not _have_database_url(),
    reason="sentinel test requires DATABASE_URL[_IPV4]; CI skip is expected",
)
async def test_survivorship_universe_population_above_floor() -> None:
    """Beyond the anchor sample, the total population of delisted
    tickers in prices_daily must clear an absolute floor.

    The audit's "<500 enumerated tickers is suspicious" rule applies
    in the opposite direction here: AFTER the structural backfill,
    delisted-row coverage should be in the low thousands, not the low
    dozens. A regression that drops the writer would land us back
    near pre-audit coverage (audit found 2/20 anchors covered, ~0.1%
    of expected breadth)."""
    import asyncpg

    db_url = os.environ.get("DATABASE_URL") or os.environ["DATABASE_URL_IPV4"]
    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        n_delisted = await conn.fetchval(
            "SELECT COUNT(DISTINCT ticker) FROM platform.prices_daily WHERE delisted = true"
        )
    finally:
        await conn.close()
    # Floor of 100: well below the expected 1000-5000, but high enough
    # that an empty/single-anchor regression reds the test. The
    # operator can tighten this once the post-merge backfill numbers
    # stabilise.
    assert (n_delisted or 0) >= 100, (
        f"only {n_delisted} delisted tickers present in prices_daily — "
        f"survivorship backfill stage did not run, or the universe "
        f"enumeration produced an empty list. Operator: re-run "
        f"``.venv/bin/python scripts/ops.py --stage historical_delisted_universe``"
    )
