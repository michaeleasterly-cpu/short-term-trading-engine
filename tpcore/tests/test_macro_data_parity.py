"""Task #18 P4 — parity verification: legacy tables ⊆ platform.macro_data.

Asserts row-by-row containment of the 3 legacy macro tables in the
corresponding source-buckets of platform.macro_data. Gate test for P5
cutover (rename _v shim views to take the original table names); P5
only proceeds when these assertions hold continuously across one full
producer cadence cycle.

DB-gated (lab-isolation-db CI job only — same fence as the cache /
schema-drift integration tests). Hits the live Supabase fixture DB
that the CI job seeds via the alembic migrations.

The contract this test enforces (HISTORY-aware — discovered live on
2026-05-24 that FRED revises published values, so the legacy table's
ON CONFLICT DO NOTHING semantics keep old values that SCD-2 has correctly
superseded with closed rows + a new current row):

  1. Every macro_indicators(indicator, date, value) row must exist as
     a macro_data row at (source='fred', series_id=indicator,
     observed_date=date, value_num=value) — current OR closed history.
  2. Every aaii_sentiment row's 3 non-NULL channels exist in macro_data
     history under source='aaii'.
  3. Every fear_greed row's up-to-8 non-NULL channels exist in
     macro_data history under source='cnn_fear_greed'.

The hy_spread sacred-data invariant retains the STRICT "current row"
contract (no revisions tolerated — operator-hand-stitched data must
never be silently superseded; if FRED publishes a different value, the
sacred test reds and the operator decides).

A non-zero "missing" count means the legacy snapshot has values that
were never observed into macro_data — cutover MUST be blocked.
"""
from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("RUN_DB_INTEGRATION_TESTS") != "1",
        reason="DB-gated; runs only in the lab-isolation-db CI job",
    ),
    pytest.mark.asyncio,
]


async def test_macro_indicators_subset_of_macro_data() -> None:
    """Every macro_indicators row matches a macro_data row at the same
    (series_id, observed_date, value_num) — current OR closed.

    History-aware contract handles the FRED-revision case: legacy stays
    on the original value (DO NOTHING on conflict), macro_data via SCD-2
    closes the original row and inserts the revised value. The original
    is preserved in history; the contract still holds.
    """
    from tpcore.db import build_asyncpg_pool

    pool = await build_asyncpg_pool(os.environ["DATABASE_URL"])
    try:
        async with pool.acquire() as conn:
            missing = await conn.fetchval(
                """
                SELECT count(*) FROM platform.macro_indicators m
                WHERE NOT EXISTS (
                    SELECT 1 FROM platform.macro_data d
                    WHERE d.source = 'fred'
                      AND d.series_id = m.indicator
                      AND d.observed_date = m.date
                      AND d.value_num = m.value
                )
                """
            )
        assert missing == 0, (
            f"{missing} macro_indicators rows have no matching macro_data "
            f"row at (source='fred', series_id=indicator, observed_date=date, "
            f"value_num=value) — current OR closed. Re-run P2 backfill if "
            f"the macro_indicators table grew new dates since last backfill."
        )
    finally:
        await pool.close()


async def test_aaii_sentiment_subset_of_macro_data() -> None:
    """Every aaii_sentiment row's 3 non-NULL channels exist in macro_data."""
    from tpcore.db import build_asyncpg_pool

    pool = await build_asyncpg_pool(os.environ["DATABASE_URL"])
    try:
        async with pool.acquire() as conn:
            # Build the expected (channel, date, val) set from aaii_sentiment;
            # confirm every one exists in macro_data.
            missing = await conn.fetchval(
                """
                WITH expected AS (
                    SELECT 'bullish_pct'::text AS channel, date, bullish_pct AS val
                        FROM platform.aaii_sentiment WHERE bullish_pct IS NOT NULL
                    UNION ALL
                    SELECT 'bearish_pct', date, bearish_pct
                        FROM platform.aaii_sentiment WHERE bearish_pct IS NOT NULL
                    UNION ALL
                    SELECT 'neutral_pct', date, neutral_pct
                        FROM platform.aaii_sentiment WHERE neutral_pct IS NOT NULL
                )
                SELECT count(*) FROM expected e
                WHERE NOT EXISTS (
                    SELECT 1 FROM platform.macro_data d
                    WHERE d.source = 'aaii'
                      AND d.series_id = e.channel
                      AND d.observed_date = e.date
                      AND d.value_num = e.val
                )
                """
            )
        assert missing == 0, (
            f"{missing} aaii_sentiment channel-observations have no matching "
            f"macro_data row at (source='aaii', series_id=channel, "
            f"observed_date=date, value_num=val) — current OR closed."
        )
    finally:
        await pool.close()


async def test_fear_greed_subset_of_macro_data() -> None:
    """Every fear_greed row's up-to-8 non-NULL channels exist in macro_data.

    6 numeric channels (score, score_5d_ago, 4 components) compared via
    value_num; 2 text channels (label, direction) compared via value_text.
    """
    from tpcore.db import build_asyncpg_pool

    pool = await build_asyncpg_pool(os.environ["DATABASE_URL"])
    try:
        async with pool.acquire() as conn:
            num_missing = await conn.fetchval(
                """
                WITH expected AS (
                    SELECT 'score'::text AS channel, date, score AS val
                        FROM platform.fear_greed WHERE score IS NOT NULL
                    UNION ALL
                    SELECT 'score_5d_ago', date, score_5d_ago
                        FROM platform.fear_greed WHERE score_5d_ago IS NOT NULL
                    UNION ALL
                    SELECT 'volatility_component', date, volatility_component
                        FROM platform.fear_greed WHERE volatility_component IS NOT NULL
                    UNION ALL
                    SELECT 'credit_component', date, credit_component
                        FROM platform.fear_greed WHERE credit_component IS NOT NULL
                    UNION ALL
                    SELECT 'momentum_component', date, momentum_component
                        FROM platform.fear_greed WHERE momentum_component IS NOT NULL
                    UNION ALL
                    SELECT 'safe_haven_component', date, safe_haven_component
                        FROM platform.fear_greed WHERE safe_haven_component IS NOT NULL
                )
                SELECT count(*) FROM expected e
                WHERE NOT EXISTS (
                    SELECT 1 FROM platform.macro_data d
                    WHERE d.source = 'cnn_fear_greed'
                      AND d.series_id = e.channel
                      AND d.observed_date = e.date
                      AND d.value_num = e.val
                )
                """
            )
            text_missing = await conn.fetchval(
                """
                WITH expected AS (
                    SELECT 'label'::text AS channel, date, label AS val
                        FROM platform.fear_greed WHERE label IS NOT NULL
                    UNION ALL
                    SELECT 'direction', date, direction
                        FROM platform.fear_greed WHERE direction IS NOT NULL
                )
                SELECT count(*) FROM expected e
                WHERE NOT EXISTS (
                    SELECT 1 FROM platform.macro_data d
                    WHERE d.source = 'cnn_fear_greed'
                      AND d.series_id = e.channel
                      AND d.observed_date = e.date
                      AND d.value_text = e.val
                )
                """
            )
        assert num_missing == 0 and text_missing == 0, (
            f"fear_greed parity gap: numeric={num_missing}, text={text_missing}"
        )
    finally:
        await pool.close()


async def test_hy_spread_sacred_preservation() -> None:
    """Sacred-data invariant: every hy_spread row in macro_indicators has
    an exact-value match in macro_data. Per project_hy_spread_sacred:
    the pre-FRED-window history (1996-2010) was hand-stitched from
    non-FRED sources and is irreplaceable."""
    from tpcore.db import build_asyncpg_pool

    pool = await build_asyncpg_pool(os.environ["DATABASE_URL"])
    try:
        async with pool.acquire() as conn:
            src_count = await conn.fetchval(
                "SELECT count(*) FROM platform.macro_indicators "
                "WHERE indicator = 'hy_spread'"
            )
            missing = await conn.fetchval(
                """
                SELECT count(*) FROM platform.macro_indicators m
                WHERE m.indicator = 'hy_spread'
                  AND NOT EXISTS (
                      SELECT 1 FROM platform.macro_data d
                      WHERE d.source='fred' AND d.series_id='hy_spread'
                        AND d.observed_date = m.date
                        AND d.value_num = m.value
                        AND d.realtime_end = 'infinity'
                  )
                """
            )
        assert missing == 0, (
            f"SACRED-DATA VIOLATION: {missing} of {src_count} hy_spread rows "
            f"in macro_indicators have no exact-value match in macro_data. "
            f"This invariant is non-negotiable per project_hy_spread_sacred."
        )
    finally:
        await pool.close()
