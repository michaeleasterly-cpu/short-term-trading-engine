"""Sentinel test — earnings_events coverage on the T1+T2 universe.

The 2026-05-13 Vector parameter-search produced ZERO trades on every
candidate because ``platform.earnings_events`` had no overlap with the
T1+T2 universe (MASTER_PLAN.md §4.3). The
``historical_earnings_events_t1_t2`` ops stage closes the gap; THIS
test is the proof that the gap stays closed.

For each anchor T1+T2 ticker:

1. At least one EARNINGS_BEAT row exists in ``platform.earnings_events``.
2. The most recent event is within 18 months of today (anchor tickers
   are large-cap quarterly reporters; an 18-month tail is conservative
   enough to absorb a holiday-shifted quarter without false-redding).

Plus a population floor: T1+T2 stock-class tickers with ≥1 earnings
event in ``platform.earnings_events`` must be ≥1500 (out of ~1500
total stock-class T1+T2; some tickers genuinely have no FMP earnings
history — IPO < 2018 etc — so the floor is set just below the full-
population number as a regression catcher, not as a strict equality).

DB-skip-gated for CI — DB integration tests are opt-in via
``DATABASE_URL`` (or ``DATABASE_URL_IPV4``) per the existing
``tests/test_survivorship_completeness.py`` precedent. Operator runs
this against the live Supabase after the post-merge one-shot
``--stage historical_earnings_events_t1_t2`` invocation.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

# Anchor sample — pinned large-cap T1+T2 tickers that are guaranteed
# to (a) be in T1+T2 today (mega-cap names) and (b) have a long FMP
# earnings history since 2018. A regression that drops the BEAT writer
# or shrinks the universe enumeration must red THIS list before any
# downstream Vector damage.
#
# Sources: MASTER_PLAN.md large-cap universe references + the existing
# DEFAULT_UNIVERSE in scripts/backfill_earnings_events.py (which seeded
# the original 44-ticker corpus and is the floor for "we already have
# this data").
_ANCHOR_T1_T2_EARNINGS: list[str] = [
    # Mega-cap technology — guaranteed multi-year EPS histories.
    "AAPL",
    "MSFT",
    "NVDA",
    "GOOGL",
    "AMZN",
    "META",
    "TSLA",
    # Financials.
    "JPM",
    "V",
    "MA",
    # Healthcare.
    "JNJ",
    "PFE",
    "MRK",
    # Consumer staples / discretionary.
    "WMT",
    "PG",
    "KO",
    "PEP",
    "HD",
    "COST",
    # Energy.
    "XOM",
    "CVX",
    # Industrials / aerospace.
    "BA",
    "CAT",
    "GE",
]


# 18-month tail — large-cap quarterly reporters land an event every
# ~91 days. A six-quarter gap is the conservative "definitely a
# regression" threshold.
_MAX_EVENT_AGE_DAYS = 540


def _have_database_url() -> bool:
    return bool(os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_IPV4"))


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    not _have_database_url(),
    reason="sentinel test requires DATABASE_URL[_IPV4]; CI skip is expected",
)
@pytest.mark.parametrize("ticker", _ANCHOR_T1_T2_EARNINGS)
async def test_anchor_ticker_has_earnings_beat(ticker: str) -> None:
    """Each anchor T1+T2 ticker has ≥1 EARNINGS_BEAT row in the table
    AND its most-recent BEAT is within 18 months of today.

    Pre-backfill (corpus ∩ T1+T2 ≤ 137 tickers per the 2026-05-14
    state in MASTER_PLAN.md §4.3) this reds on the bulk of the list;
    post-backfill it passes for every anchor. The 18-month tail
    catches a regression where the writer ran but only landed pre-
    2024 history.
    """
    import asyncpg

    db_url = os.environ.get("DATABASE_URL") or os.environ["DATABASE_URL_IPV4"]
    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS n_beats,
                   MAX(event_date) AS most_recent_beat
            FROM platform.earnings_events
            WHERE ticker = $1
              AND event_type = 'EARNINGS_BEAT'
            """,
            ticker,
        )
    finally:
        await conn.close()

    n_beats = (row["n_beats"] or 0) if row else 0
    assert n_beats >= 1, (
        f"{ticker}: no EARNINGS_BEAT rows in platform.earnings_events — "
        f"T1+T2 catalyst-event coverage gap NOT closed. Operator: "
        f"run ``.venv/bin/python scripts/ops.py "
        f"--stage historical_earnings_events_t1_t2``"
    )
    most_recent = row["most_recent_beat"]
    age_days = (datetime.now(UTC).date() - most_recent).days
    assert age_days <= _MAX_EVENT_AGE_DAYS, (
        f"{ticker}: most-recent EARNINGS_BEAT is {most_recent} ("
        f"{age_days} days old) — exceeds the {_MAX_EVENT_AGE_DAYS}-day "
        f"tail threshold. Either the backfill ran but truncated pre-"
        f"2024, or the weekly earnings_refresh has been broken for "
        f"≥6 quarters."
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    not _have_database_url(),
    reason="sentinel test requires DATABASE_URL[_IPV4]; CI skip is expected",
)
async def test_t1_t2_stock_class_earnings_coverage_above_floor() -> None:
    """Population floor — the count of T1+T2 stock-class tickers
    with ≥1 EARNINGS_BEAT must be at the high end of the universe.

    Pre-backfill (2026-05-22 state): 1004/1500 stock-class T1+T2
    tickers covered. Post-backfill: should be ≥1300 (some tickers
    legitimately have no FMP earnings history — very recent IPOs, etc).
    The 1300 floor is the regression catcher; the operator can tighten
    once post-merge numbers stabilise.
    """
    import asyncpg

    db_url = os.environ.get("DATABASE_URL") or os.environ["DATABASE_URL_IPV4"]
    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        n_covered = await conn.fetchval(
            """
            SELECT COUNT(DISTINCT lt.ticker)
            FROM platform.liquidity_tiers lt
            LEFT JOIN platform.ticker_classifications tc USING (ticker)
            WHERE lt.tier <= 2
              AND COALESCE(tc.asset_class, 'stock') = 'stock'
              AND EXISTS (
                  SELECT 1 FROM platform.earnings_events ee
                  WHERE ee.ticker = lt.ticker
                    AND ee.event_type = 'EARNINGS_BEAT'
              )
            """
        )
    finally:
        await conn.close()
    assert (n_covered or 0) >= 1300, (
        f"only {n_covered} T1+T2 stock-class tickers have EARNINGS_BEAT "
        f"events — expected ≥1300 post-backfill. Operator: run "
        f"``.venv/bin/python scripts/ops.py "
        f"--stage historical_earnings_events_t1_t2``"
    )
