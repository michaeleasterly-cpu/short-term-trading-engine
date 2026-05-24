"""Corporate-history P2 (thinned) — tests for tpcore.corp_history.resolve_issuer_at_date.

DB-gated (lab-isolation-db CI job only). Hits live Supabase fixture DB.

The helper walks two SCD-2 hops:
  ticker → classification_id (via ticker_history)
       → issuer_id (via issuer_securities)

Both hops use valid_from <= as_of <= valid_to semantics so historical
bars resolve to the historical issuer even after renames / mergers.

Tests use the seeded TWTR row (issuer_id=CIK0001418091) from the
corporate_events_seed stage. Test data is read-only; no INSERTs.
"""
from __future__ import annotations

import os
from datetime import date

import pytest

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("RUN_DB_INTEGRATION_TESTS") != "1",
        reason="DB-gated; runs only in the lab-isolation-db CI job",
    ),
    pytest.mark.asyncio,
]


async def test_resolve_known_ticker_returns_issuer_id() -> None:
    """A ticker that exists in ticker_history + has issuer_securities mapping
    resolves to its issuer_id."""
    from tpcore.corp_history import resolve_issuer_at_date
    from tpcore.db import build_asyncpg_pool

    pool = await build_asyncpg_pool(os.environ["DATABASE_URL"])
    try:
        async with pool.acquire() as conn:
            # Find a ticker that's in both ticker_history AND issuer_securities.
            row = await conn.fetchrow(
                """
                SELECT th.ticker, iss.issuer_id, th.valid_from
                FROM platform.ticker_history th
                JOIN platform.issuer_securities iss
                  ON iss.classification_id = th.classification_id
                LIMIT 1
                """
            )
            if row is None:
                pytest.skip("no seeded ticker yet — run corporate_events_seed first")

            ticker = row["ticker"]
            expected_issuer = row["issuer_id"]
            valid_from = row["valid_from"]
            # Resolve at a date AFTER the valid_from so the SCD-2 window includes it.
            from datetime import timedelta
            as_of = valid_from + timedelta(days=30)

            actual = await resolve_issuer_at_date(conn, ticker, as_of)
            assert actual == expected_issuer, (
                f"ticker={ticker} as_of={as_of} expected {expected_issuer}, got {actual}"
            )
    finally:
        await pool.close()


async def test_resolve_unknown_ticker_returns_none() -> None:
    """A ticker that isn't in ticker_history returns None (not an error)."""
    from tpcore.corp_history import resolve_issuer_at_date
    from tpcore.db import build_asyncpg_pool

    pool = await build_asyncpg_pool(os.environ["DATABASE_URL"])
    try:
        async with pool.acquire() as conn:
            result = await resolve_issuer_at_date(
                conn, "ZZZ_NOT_A_REAL_TICKER_FROM_TEST", date(2026, 1, 1),
            )
        assert result is None
    finally:
        await pool.close()
