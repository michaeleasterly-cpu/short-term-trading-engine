"""Tests for the P4 provenance-downgrade guard on ``_upsert_bars``.

2026-05-25 trust-audit: the ON CONFLICT DO UPDATE clause used to
unconditionally overwrite ``prices_daily.source``, letting a legacy
``alpaca`` writer silently downgrade a row already tagged ``fmp``
(the operator-stated primary). Now the UPDATE is gated by
``platform._source_priority(EXCLUDED.source) >=
platform._source_priority(platform.prices_daily.source)``.

These tests pin the SQL contract — both the WHERE clause exists and
the priority ordering is what the audit + memory called for.
"""

from __future__ import annotations

import pytest

from tpcore.data import ingest_alpaca_bars


def test_upsert_sql_has_source_priority_where_clause() -> None:
    """The provenance-guard WHERE clause must be in the production
    SQL — without it, a legacy lower-priority writer silently
    overwrites authoritative provenance. Sentinel for the P4
    contract."""
    # The SQL is built inline in _upsert_bars. Read the function's
    # source so the assertion catches accidental regressions of the
    # guard's WHERE clause.
    import inspect
    src = inspect.getsource(ingest_alpaca_bars._upsert_bars)
    assert "ON CONFLICT (ticker, date) DO UPDATE" in src
    assert "_source_priority(EXCLUDED.source)" in src
    assert "_source_priority(platform.prices_daily.source)" in src


# ─────────────────────────────────────────────────────────────────────
# Priority ordering — pinned against memory
# project_fmp_primary_daily_bars_2026_05_22 + the migration.
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "lower,higher",
    [
        ("tradier", "iex"),       # tradier (frozen) is lowest
        ("iex", "alpaca"),        # alpaca peer to sip > iex
        ("iex", "sip"),
        ("alpaca", "fmp"),        # FMP is primary
        ("sip", "fmp"),
        ("tradier", "fmp"),       # any → fmp upgrade allowed
        ("unknown_value", "iex"), # ELSE branch = 0, lowest
    ],
)
def test_source_priority_ordering(lower: str, higher: str) -> None:
    """Pin the priority ordering via a live-DB query of the function
    (the function lives in the migration, not Python — so this test
    asserts the function is installed correctly + ranks as expected).

    Marked DB-gated so CI without a live DB doesn't false-fail; the
    invariant is the operator memory `project_fmp_primary_daily_bars_2026_05_22`
    + the migration `20260525_0700`."""
    import asyncio
    import os
    pytest.importorskip("asyncpg")
    import asyncpg
    db_url = os.getenv("DATABASE_URL_IPV4") or os.getenv("DATABASE_URL")
    if not db_url:
        pytest.skip("no DATABASE_URL — provenance ordering test is DB-gated")

    async def _check():
        conn = await asyncpg.connect(db_url)
        try:
            r_lower = await conn.fetchval(
                "SELECT platform._source_priority($1)", lower,
            )
            r_higher = await conn.fetchval(
                "SELECT platform._source_priority($1)", higher,
            )
        finally:
            await conn.close()
        return int(r_lower), int(r_higher)

    lo, hi = asyncio.run(_check())
    assert lo < hi, f"{lower}={lo} not strictly less than {higher}={hi}"


def test_source_priority_same_source_is_equal() -> None:
    """A fresh fmp pull over an existing fmp row IS allowed (same
    priority = equal, not less-than). This is the legitimate refresh
    case the WHERE >= clause permits."""
    import asyncio
    import os
    pytest.importorskip("asyncpg")
    import asyncpg
    db_url = os.getenv("DATABASE_URL_IPV4") or os.getenv("DATABASE_URL")
    if not db_url:
        pytest.skip("no DATABASE_URL — same-priority test is DB-gated")

    async def _check():
        conn = await asyncpg.connect(db_url)
        try:
            return int(await conn.fetchval(
                "SELECT platform._source_priority('fmp')"
                " - platform._source_priority('fmp')"
            ))
        finally:
            await conn.close()

    assert asyncio.run(_check()) == 0
