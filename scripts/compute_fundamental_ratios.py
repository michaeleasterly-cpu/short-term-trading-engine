"""Compute point-in-time P/B and D/E for every cached quarterly filing.

Reads ``platform.fundamentals_quarterly`` rows that don't yet have
``pb``/``de`` populated, joins each row to the closing price on its
``filing_date`` (or the most recent prior trading day), and writes the
two ratios back. Idempotent: rows where ``pb`` and ``de`` are already
filled are skipped on subsequent runs.

Definitions::

    book_value_per_share = (total_assets − total_liabilities) / shares_outstanding
    pb = close / book_value_per_share
    de = total_liabilities / (total_assets − total_liabilities)

Edge cases (skipped — no row written, ratio left NULL):

* missing ``total_assets`` / ``total_liabilities`` / ``shares_outstanding``
* book value (assets − liabilities) ≤ 0  → P/B undefined and D/E undefined
* shares ≤ 0  → P/B undefined
* no price row in ``platform.prices_daily`` on or before ``filing_date``

Run::

    python scripts/compute_fundamental_ratios.py
    python scripts/compute_fundamental_ratios.py --force   # overwrite existing pb/de
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from tpcore.db import build_asyncpg_pool

logger = logging.getLogger("scripts.compute_fundamental_ratios")


# Set-based UPDATE: for each (ticker, filing_date) needing pb/de, pick the
# most recent prices_daily close on-or-before filing_date and compute both
# ratios. One SQL statement so it scales to 100k+ rows without holding a
# pool connection long enough for the Supabase pooler to drop it.
#
# Why total_assets > 0 AND total_liabilities >= 0 (not just (ta - tl) > 0):
# FMP occasionally returns degenerate rows with ta = 0 and tl < 0 (the
# accounting is inverted — e.g. ARX 2024-03-31). Those satisfy
# (ta - tl) > 0 because 0 - (-x) > 0, but produce a de of -1.0 because
# tl / book = -x / x. The tightened predicates reject those rows up front
# so we never write the bogus ratios. 51 rows had this shape post-backfill
# and were nulled by a one-shot cleanup; this filter keeps re-runs clean.
#
# Postgres has no Decimal-style ``.quantize(Decimal("0.000001"))``; we use
# ``round(..., 6)`` which matches at NUMERIC precision.
_UPDATE_SQL = """
    WITH targets AS (
        SELECT ticker, filing_date, total_assets, total_liabilities, shares_outstanding
        FROM platform.fundamentals_quarterly
        WHERE total_assets IS NOT NULL
          AND total_liabilities IS NOT NULL
          AND shares_outstanding IS NOT NULL
          AND total_assets > 0
          AND total_liabilities >= 0
          AND shares_outstanding > 0
          AND (total_assets - total_liabilities) > 0
          {where}
    ),
    priced AS (
        SELECT DISTINCT ON (t.ticker, t.filing_date)
            t.ticker, t.filing_date, t.total_assets, t.total_liabilities,
            t.shares_outstanding, pd.close
        FROM targets t
        JOIN platform.prices_daily pd
          ON pd.ticker = t.ticker AND pd.date <= t.filing_date
        ORDER BY t.ticker, t.filing_date, pd.date DESC
    )
    UPDATE platform.fundamentals_quarterly fq
    SET pb = round(p.close / ((p.total_assets - p.total_liabilities) / p.shares_outstanding), 6),
        de = round(p.total_liabilities / (p.total_assets - p.total_liabilities), 6)
    FROM priced p
    WHERE fq.ticker = p.ticker
      AND fq.filing_date = p.filing_date
    RETURNING fq.ticker
"""


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    where = "" if args.force else "AND (pb IS NULL OR de IS NULL)"
    sql = _UPDATE_SQL.format(where=where)

    pool = await build_asyncpg_pool(db_url)
    try:
        logger.info("compute_ratios.start force=%s", args.force)
        async with pool.acquire() as conn:
            updated = await conn.fetch(sql)
            populated = await conn.fetchrow(
                "SELECT COUNT(*) FILTER (WHERE pb IS NOT NULL) AS pb_n, "
                "COUNT(*) FILTER (WHERE de IS NOT NULL) AS de_n, "
                "COUNT(*) AS total FROM platform.fundamentals_quarterly"
            )
    finally:
        await pool.close()

    print()
    print(f"Rows updated this run: {len(updated)}")
    print(
        f"fundamentals_quarterly: {populated['total']} rows total, "
        f"pb populated on {populated['pb_n']}, de populated on {populated['de_n']}"
    )
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--force", action="store_true",
        help="Recompute pb/de even if they're already populated.",
    )
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
