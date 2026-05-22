"""Sentinel test — insider_filings coverage on the T1+T2 universe.

The 2026-05-22 Carver-driven vector engine candidate
``vector_beat_reversal_insider_filter_v1`` requires the FMP-sourced
daily-granularity insider-filings table. As of the operator-on-demand
``historical_insider_sentiment_daily`` resume run at the time of this
sentinel landing, coverage stood at ~16% of the T1+T2 stock universe;
this test gates the post-backfill completion: ≥90% of T1+T2 stock-
class symbols must have at least one row in
``platform.insider_filings``.

The 90% floor reflects the structural reality that:

* A small minority of T1+T2 stocks legitimately have no Form-4 filings
  in the operator's 2018-01-01+ horizon (very recent IPOs, foreign
  issuers exempt from §16 reporting, etc.).
* FMP's tier coverage on /stable/insider-trading/search has occasional
  per-symbol gaps that drop a few names per run.

Setting the floor at 90% catches the load-bearing regression (the
backfill failed to land at all, or the universe enumeration shrank to
a sub-population) without false-redding on the long tail of "no
filings yet exist" tickers.

DB-skip-gated for CI per the same pattern as
``tests/test_earnings_events_t1_t2_coverage.py`` and
``tests/test_survivorship_completeness.py``. Operator runs against the
live Supabase after the resume run completes.
"""
from __future__ import annotations

import os

import pytest

# Floor for "T1+T2 stock-class symbols with ≥1 insider filing".
# 90% is the operator-spec post-backfill threshold; the underlying
# T1+T2 stock population is ~1500 tickers, so the absolute floor is
# ~1350.
_MIN_COVERAGE_PCT = 0.90


def _have_database_url() -> bool:
    return bool(
        os.environ.get("DATABASE_URL")
        or os.environ.get("DATABASE_URL_IPV4"),
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    not _have_database_url(),
    reason=(
        "sentinel test requires DATABASE_URL[_IPV4]; "
        "CI skip is expected"
    ),
)
async def test_t1_t2_insider_filings_coverage_above_floor() -> None:
    """≥90% of T1+T2 stock-class symbols have ≥1 row in
    ``platform.insider_filings`` post-backfill.

    Pre-backfill (resume-run-in-progress 2026-05-22): ~16% coverage —
    expected, the backfill is mid-run. Post-backfill: ≥90% of the
    ~1500 T1+T2 stock population. A regression that drops below the
    floor surfaces a Form-4 ingest defect.
    """
    import asyncpg

    db_url = (
        os.environ.get("DATABASE_URL")
        or os.environ["DATABASE_URL_IPV4"]
    )
    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        row = await conn.fetchrow(
            """
            WITH t1_t2_stocks AS (
                SELECT lt.ticker
                FROM platform.liquidity_tiers lt
                LEFT JOIN platform.ticker_classifications tc
                  USING (ticker)
                WHERE lt.tier <= 2
                  AND COALESCE(tc.asset_class, 'stock') = 'stock'
            )
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (
                    WHERE EXISTS (
                        SELECT 1 FROM platform.insider_filings ifg
                        WHERE ifg.symbol = t1_t2_stocks.ticker
                    )
                ) AS covered
            FROM t1_t2_stocks
            """,
        )
    finally:
        await conn.close()

    total = int(row["total"] or 0) if row else 0
    covered = int(row["covered"] or 0) if row else 0
    assert total > 0, (
        "no T1+T2 stock-class tickers found — liquidity_tiers and/or "
        "ticker_classifications appear empty; check the prior data-"
        "operations cycle."
    )
    coverage_pct = covered / total
    assert coverage_pct >= _MIN_COVERAGE_PCT, (
        f"insider_filings T1+T2 stock coverage = {coverage_pct:.2%} "
        f"({covered}/{total}) — below the {_MIN_COVERAGE_PCT:.0%} "
        f"floor. Operator: run ``.venv/bin/python scripts/ops.py "
        f"--stage historical_insider_sentiment_daily`` and let it "
        f"complete (resumable via INSIDER_BACKFILL_SYMBOL_DONE events)."
    )
