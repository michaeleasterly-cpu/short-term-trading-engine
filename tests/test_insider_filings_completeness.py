"""Sentinel test — daily-granularity insider-filings coverage in
``platform.insider_filings``.

Carver-driven 2026-05-22: the vector engine candidate
``vector_beat_reversal_insider_filter_v1`` needs a 30d-rolling MSPR
signal at DAILY resolution. The
``historical_insider_sentiment_daily`` ops stage backfills it; this
test is the proof the gap stays closed.

For a small anchor sample (large-cap names with continuous Form-4
filing activity):

1. ≥ N rows exist in ``platform.insider_filings`` for the ticker.
2. The earliest ``transaction_date`` is ≤ 2019-01-01 (the backfill
   horizon is 2018-01-01; one full year of slack for FMP gaps).
3. The latest ``transaction_date`` is within the last 90 days (proves
   the nightly delta is landing fresh rows).

DB-skip-gated for CI: opt-in via ``DATABASE_URL`` per the existing
``tests/test_survivorship_completeness.py`` precedent. The operator
runs this against the live Supabase after the post-merge one-shot
``--stage historical_insider_sentiment_daily`` invocation.
"""
from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta

import pytest

# Anchor names — large-cap continuous Form-4 filers. Conservative
# floor for row counts (AAPL/MSFT typically have 200+ filings/year).
_ANCHOR_TICKERS_WITH_FLOOR: list[tuple[str, int]] = [
    ("AAPL", 200),
    ("MSFT", 200),
    ("NVDA", 100),
    ("TSLA", 100),
    ("JPM", 100),
]


def _have_database_url() -> bool:
    return bool(
        os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_IPV4"),
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    not _have_database_url(),
    reason="sentinel test requires DATABASE_URL[_IPV4]; CI skip is expected",
)
@pytest.mark.parametrize("ticker, min_rows", _ANCHOR_TICKERS_WITH_FLOOR)
async def test_anchor_insider_filings_present(
    ticker: str, min_rows: int,
) -> None:
    """Each anchor ticker has ≥ min_rows insider filings spanning at
    least 2018-2019 → today−90d. Proves both the structural backfill
    and the nightly delta are landing rows."""
    import asyncpg

    db_url = os.environ.get("DATABASE_URL") or os.environ["DATABASE_URL_IPV4"]
    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS n,
                   MIN(transaction_date) AS first_tx,
                   MAX(transaction_date) AS last_tx
            FROM platform.insider_filings
            WHERE symbol = $1
            """,
            ticker,
        )
        if row is None or (row["n"] or 0) == 0:
            pytest.fail(
                f"{ticker}: no rows in platform.insider_filings — "
                f"daily-granularity gap not closed. Operator: run "
                f"``.venv/bin/python scripts/ops.py --stage "
                f"historical_insider_sentiment_daily``"
            )

        assert (row["n"] or 0) >= min_rows, (
            f"{ticker}: only {row['n']} rows — expected ≥ {min_rows}. "
            f"The backfill landed only a partial slice. Re-run the stage."
        )

        # Predicate 2: history reaches at least 2019.
        assert row["first_tx"] <= date(2019, 1, 1), (
            f"{ticker}: earliest transaction_date is {row['first_tx']} — "
            f"backfill horizon is 2018-01-01 (one year slack)."
        )

        # Predicate 3: nightly delta is fresh (within 90 days of today).
        today = datetime.now(UTC).date()
        assert (today - row["last_tx"]).days <= 90, (
            f"{ticker}: latest transaction_date is {row['last_tx']} "
            f"({(today - row['last_tx']).days}d ago) — daily delta is "
            f"NOT landing fresh rows. Check the data-operations daemon."
        )
    finally:
        await conn.close()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    not _have_database_url(),
    reason="sentinel test requires DATABASE_URL[_IPV4]; CI skip is expected",
)
async def test_insider_filings_population_above_floor() -> None:
    """Beyond the anchor sample, the total population of distinct
    symbols with insider filings must clear an absolute floor. A
    regression that drops the writer would land us back at zero."""
    import asyncpg

    db_url = os.environ.get("DATABASE_URL") or os.environ["DATABASE_URL_IPV4"]
    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        n_symbols = await conn.fetchval(
            "SELECT COUNT(DISTINCT symbol) FROM platform.insider_filings"
        )
    finally:
        await conn.close()
    # Floor of 500: well below the expected ~2000-2400 (full T1+T2
    # universe), but high enough that an empty / single-symbol
    # regression reds the test.
    assert (n_symbols or 0) >= 500, (
        f"only {n_symbols} symbols have insider filings — the daily-"
        f"granularity backfill did not run, or it ran for <500 symbols. "
        f"Operator: re-run ``.venv/bin/python scripts/ops.py --stage "
        f"historical_insider_sentiment_daily``."
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    not _have_database_url(),
    reason="sentinel test requires DATABASE_URL[_IPV4]; CI skip is expected",
)
async def test_insider_filings_30d_rolling_mspr_computable() -> None:
    """The end-to-end test: can the vector engine actually compute a
    30d-rolling MSPR from this table? Proves the table shape is
    fit-for-purpose, not just present."""
    import asyncpg

    db_url = os.environ.get("DATABASE_URL") or os.environ["DATABASE_URL_IPV4"]
    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    today = datetime.now(UTC).date()
    since = today - timedelta(days=30)
    try:
        mspr = await conn.fetchval(
            """
            WITH window_rows AS (
                SELECT acquisition_or_disposition, securities_transacted, price
                FROM platform.insider_filings
                WHERE symbol = 'AAPL'
                  AND transaction_date BETWEEN $1::date AND $2::date
                  AND price > 0
            )
            SELECT
                CASE
                    WHEN SUM(securities_transacted * price) = 0 THEN 0
                    ELSE 100.0 * (
                        SUM(CASE WHEN acquisition_or_disposition = 'A'
                                 THEN securities_transacted * price ELSE 0 END)
                      - SUM(CASE WHEN acquisition_or_disposition = 'D'
                                 THEN securities_transacted * price ELSE 0 END)
                    ) / NULLIF(SUM(securities_transacted * price), 0)
                END AS mspr
            FROM window_rows
            """,
            since,
            today,
        )
    finally:
        await conn.close()
    # The MSPR value itself isn't pinned (it floats); we just assert
    # the computation runs and stays in [-100, 100] (the algebraic range).
    if mspr is None:
        # An empty window for AAPL in the last 30 days is suspicious but
        # legal — skip rather than fail (FMP can have a window with no
        # filings during an insider trading restricted period).
        pytest.skip("AAPL had no insider filings in the past 30 days")
    assert -100 <= float(mspr) <= 100, (
        f"AAPL 30d MSPR = {mspr} — outside [-100, 100], computation broke"
    )
