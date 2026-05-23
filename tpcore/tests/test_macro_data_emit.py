"""Task #18 P3 — tests for the bitemporal SCD-2 macro_data double-write helper.

DB-gated (runs only in the lab-isolation-db CI job — same fence as the
fundamentals-cache integration test). Verifies the three SCD-2 states:

  inserted  — fresh observation, no current row exists.
  no_change — current row's value matches the new value; NO writes.
  revised   — value differs; old current row gets realtime_end := now(),
              new row inserted with realtime_start := now().

After all operations, the "exactly one current row per natural key"
invariant must hold (the bitemporal contract that legacy consumers
reading the _v shim views depend on).

Uses a synthetic source identifier ``test_p3_helper`` that no producer
ever emits, so the test cannot pollute real macro_data.
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

import pytest

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("RUN_DB_INTEGRATION_TESTS") != "1",
        reason="DB-gated; runs only in the lab-isolation-db CI job",
    ),
    pytest.mark.asyncio,
]

_TEST_SOURCE = "test_p3_helper"


async def _clean(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM platform.macro_data WHERE source = $1", _TEST_SOURCE,
        )


async def test_scd2_insert_no_change_revise_cycle() -> None:
    """Full SCD-2 cycle: insert → no-change → revise. Verify counts +
    the one-current-row-per-natural-key invariant + closed-row history."""
    from tpcore.db import build_asyncpg_pool
    from tpcore.ingestion.macro_data_emit import upsert_macro_data_bitemporal

    pool = await build_asyncpg_pool(os.environ["DATABASE_URL"])
    try:
        await _clean(pool)

        async with pool.acquire() as conn:
            # 1. Fresh insert — 3 rows, all new.
            r1 = await upsert_macro_data_bitemporal(
                conn, source=_TEST_SOURCE,
                rows=[
                    ("vol",   date(2026, 5, 1), Decimal("18.5"), None),
                    ("vol",   date(2026, 5, 2), Decimal("19.0"), None),
                    ("label", date(2026, 5, 1), None,            "Greed"),
                ],
            )
            assert r1 == {"inserted": 3, "revised": 0, "no_change": 0}

            # 2. Re-emit same — bitemporal no-op.
            r2 = await upsert_macro_data_bitemporal(
                conn, source=_TEST_SOURCE,
                rows=[
                    ("vol",   date(2026, 5, 1), Decimal("18.5"), None),
                    ("vol",   date(2026, 5, 2), Decimal("19.0"), None),
                    ("label", date(2026, 5, 1), None,            "Greed"),
                ],
            )
            assert r2 == {"inserted": 0, "revised": 0, "no_change": 3}

            # 3. Mixed: 2 values changed, 1 unchanged.
            r3 = await upsert_macro_data_bitemporal(
                conn, source=_TEST_SOURCE,
                rows=[
                    ("vol",   date(2026, 5, 1), Decimal("99.9"), None),    # revised
                    ("vol",   date(2026, 5, 2), Decimal("19.0"), None),    # no_change
                    ("label", date(2026, 5, 1), None,            "Fear"),  # revised
                ],
            )
            assert r3 == {"inserted": 0, "revised": 2, "no_change": 1}

            # 4. Exactly one current row per natural key.
            current = await conn.fetch(
                """
                SELECT series_id, observed_date, value_num, value_text
                FROM platform.macro_data
                WHERE source = $1 AND realtime_end = 'infinity'
                ORDER BY series_id, observed_date
                """,
                _TEST_SOURCE,
            )
            assert len(current) == 3, current

            current_by_key = {
                (r["series_id"], r["observed_date"]): (r["value_num"], r["value_text"])
                for r in current
            }
            assert current_by_key[("label", date(2026, 5, 1))] == (None, "Fear")
            assert current_by_key[("vol",   date(2026, 5, 1))] == (Decimal("99.9"), None)
            assert current_by_key[("vol",   date(2026, 5, 2))] == (Decimal("19.0"), None)

            # 5. Closed-row history: each revised key has exactly one closed row.
            history = await conn.fetch(
                """
                SELECT series_id, observed_date, value_num, value_text,
                       realtime_end = 'infinity' AS is_current
                FROM platform.macro_data
                WHERE source = $1
                ORDER BY series_id, observed_date, realtime_start
                """,
                _TEST_SOURCE,
            )
            assert len(history) == 5, history
            closed = [r for r in history if not r["is_current"]]
            assert len(closed) == 2

            # The closed row for each revised key holds the OLD value
            # (Greed for label, 18.5 for vol@2026-05-01).
            closed_by_key = {
                (r["series_id"], r["observed_date"]): (r["value_num"], r["value_text"])
                for r in closed
            }
            assert closed_by_key[("label", date(2026, 5, 1))] == (None, "Greed")
            assert closed_by_key[("vol",   date(2026, 5, 1))] == (Decimal("18.5"), None)
    finally:
        await _clean(pool)
        await pool.close()


async def test_empty_input_is_noop() -> None:
    """Empty rows list must not hit the DB and must not error."""
    from tpcore.db import build_asyncpg_pool
    from tpcore.ingestion.macro_data_emit import upsert_macro_data_bitemporal

    pool = await build_asyncpg_pool(os.environ["DATABASE_URL"])
    try:
        async with pool.acquire() as conn:
            r = await upsert_macro_data_bitemporal(conn, source="never", rows=[])
        assert r == {"inserted": 0, "revised": 0, "no_change": 0}
    finally:
        await pool.close()
