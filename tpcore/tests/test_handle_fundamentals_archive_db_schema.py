"""DB-gated schema-drift proof for ``fmp_fundamentals`` CSV archive.

Closes the second half of the operator's verification task:
the existing in-memory ``test_handle_fundamentals_archive_e2e`` asserts
the CSV header equals ``handlers.FUNDAMENTALS_ARCHIVE_FIELDS`` — that
pair of tests is a sandwich whose middle layer (the canonical tuple)
is pinned to the live ``platform.fundamentals_quarterly`` schema by
THIS test.

The invariant: every data column on ``platform.fundamentals_quarterly``
(everything except the surrogate primary-key ``id``) MUST appear in
``FUNDAMENTALS_ARCHIVE_FIELDS``. A future migration that adds a column
(e.g. ``gross_profit``) but doesn't update the archive tuple will fail
this test — the archive would silently drop the new column on every
refresh, defeating the "CSV can fully reconstruct DB state if FMP
revokes history" invariant in ``handle_fundamentals_refresh``.

Bidirectional pin:
- All DB columns (minus ``id``) must be in the archive tuple
  (catches "added a DB column but not the archive").
- All archive-tuple entries must be real DB columns (catches a typo
  or stale entry — the archive writes empty strings for missing keys
  and we'd never notice without an explicit pin).

DB-gated: skip outside the ``lab-isolation-db`` CI job (no real
Postgres + applied migrations to inspect). The fence mirrors the
existing DB-gated tests under ``tpcore/tests/`` (``test_lab_isolation``,
``test_persistent_store``, etc.).
"""
from __future__ import annotations

import os

import pytest

# Same fence the other DB-gated tests use: lab-isolation-db CI job sets
# RUN_DB_INTEGRATION_TESTS=1; local + the non-DB CI job leave it unset.
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_INTEGRATION_TESTS") != "1",
    reason="DB-gated; runs only in the lab-isolation-db CI job",
)

# The ``id`` PK is a surrogate (auto-incremented BigInteger introduced
# in 20260510_0049_create_fundamentals_quarterly) — it has no semantic
# meaning and the archive deliberately omits it. Any other column on
# the table is real data and MUST be archived.
_SURROGATE_COLUMNS: frozenset[str] = frozenset({"id"})


async def test_fundamentals_archive_fields_matches_live_db_schema() -> None:
    """``FUNDAMENTALS_ARCHIVE_FIELDS`` must equal the live data-column
    set of ``platform.fundamentals_quarterly`` (PK ``id`` excluded).

    Catches schema drift in both directions: a DB column not yet
    listed in the archive tuple (the original "missing fmp_fundamentals
    CSV archive — presence unproven" concern, generalized) AND a
    typo/stale entry in the tuple that doesn't correspond to a real
    column."""
    from tpcore.db import build_asyncpg_pool
    from tpcore.ingestion.handlers import FUNDAMENTALS_ARCHIVE_FIELDS

    db_url = os.environ.get("DATABASE_URL")
    assert db_url, "DATABASE_URL not set — required for DB-gated test"

    pool = await build_asyncpg_pool(db_url)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'platform'
                  AND table_name = 'fundamentals_quarterly'
                ORDER BY ordinal_position
                """
            )
    finally:
        await pool.close()

    assert rows, (
        "platform.fundamentals_quarterly has no columns — alembic upgrade "
        "did not run, or the table was dropped. The lab-isolation-db CI "
        "job's 'Apply alembic migrations' step is the prerequisite."
    )
    live_columns = {r["column_name"] for r in rows}
    live_data_columns = live_columns - _SURROGATE_COLUMNS

    archive_set = set(FUNDAMENTALS_ARCHIVE_FIELDS)

    missing_from_archive = live_data_columns - archive_set
    assert not missing_from_archive, (
        f"platform.fundamentals_quarterly has data columns that are NOT "
        f"in FUNDAMENTALS_ARCHIVE_FIELDS — the CSV archive would silently "
        f"drop them. Add to tpcore.ingestion.handlers.FUNDAMENTALS_ARCHIVE_FIELDS "
        f"(and confirm handle_fundamentals_refresh's SELECT covers them).\n"
        f"  missing: {sorted(missing_from_archive)}"
    )
    unknown_in_archive = archive_set - live_data_columns
    assert not unknown_in_archive, (
        f"FUNDAMENTALS_ARCHIVE_FIELDS lists columns that DO NOT exist on "
        f"platform.fundamentals_quarterly — typo or stale entry. The "
        f"archive writer silently writes empty strings for these, hiding "
        f"the gap. Either add the column via migration or remove the "
        f"entry.\n  unknown: {sorted(unknown_in_archive)}"
    )
