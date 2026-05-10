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
from decimal import Decimal

from tpcore.db import build_asyncpg_pool

logger = logging.getLogger("scripts.compute_fundamental_ratios")


_FETCH_PRICE_SQL = """
    SELECT close
    FROM platform.prices_daily
    WHERE ticker = $1 AND date <= $2
    ORDER BY date DESC
    LIMIT 1
"""

_FETCH_FUNDS_SQL = """
    SELECT ticker, filing_date, total_assets, total_liabilities, shares_outstanding
    FROM platform.fundamentals_quarterly
    {where}
    ORDER BY ticker, filing_date
"""

_UPDATE_SQL = """
    UPDATE platform.fundamentals_quarterly
    SET pb = $1, de = $2
    WHERE ticker = $3 AND filing_date = $4
"""


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    where = "" if args.force else "WHERE pb IS NULL OR de IS NULL"
    sql = _FETCH_FUNDS_SQL.format(where=where)

    pool = await build_asyncpg_pool(db_url)
    counts = {"computed": 0, "skipped_no_balance": 0, "skipped_no_book_value": 0,
              "skipped_no_shares": 0, "skipped_no_price": 0}
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql)
            logger.info("compute_ratios.input rows=%d (force=%s)", len(rows), args.force)

            for r in rows:
                ticker = r["ticker"]
                filing = r["filing_date"]
                ta = r["total_assets"]
                tl = r["total_liabilities"]
                sh = r["shares_outstanding"]
                if ta is None or tl is None:
                    counts["skipped_no_balance"] += 1
                    continue
                book_value = Decimal(str(ta)) - Decimal(str(tl))
                if book_value <= 0:
                    counts["skipped_no_book_value"] += 1
                    continue
                if sh is None or Decimal(str(sh)) <= 0:
                    counts["skipped_no_shares"] += 1
                    continue
                bvps = book_value / Decimal(str(sh))
                close_row = await conn.fetchrow(_FETCH_PRICE_SQL, ticker, filing)
                if close_row is None:
                    counts["skipped_no_price"] += 1
                    continue
                close = Decimal(str(close_row["close"]))
                pb = (close / bvps).quantize(Decimal("0.000001"))
                de = (Decimal(str(tl)) / book_value).quantize(Decimal("0.000001"))
                await conn.execute(_UPDATE_SQL, pb, de, ticker, filing)
                counts["computed"] += 1

            populated = await conn.fetchrow(
                "SELECT COUNT(*) FILTER (WHERE pb IS NOT NULL) AS pb_n, "
                "COUNT(*) FILTER (WHERE de IS NOT NULL) AS de_n, "
                "COUNT(*) AS total FROM platform.fundamentals_quarterly"
            )
    finally:
        await pool.close()

    print()
    print("Computation summary:")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    print(
        f"\nfundamentals_quarterly: {populated['total']} rows total, "
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
