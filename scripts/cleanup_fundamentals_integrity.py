"""One-shot cleanup for fundamentals_quarterly integrity issues.

Two violations:
  1. period_end_date > filing_date — physically impossible (filings come
     AFTER the period they cover). Off-by-one artifact in legacy ingests.
     Fix: DELETE the rows; wrong filing_date causes look-ahead bias in
     point-in-time backtests. Better to have no row than a wrong one.
  2. shares_outstanding == 0 or < 0 — placeholder/garbage. Fix: UPDATE
     to NULL so engines treat it as "unknown" rather than divide-by-zero.

Both writes are wrapped in a transaction and audit-logged to
``platform.application_log`` (event_type=DATA_CLEANUP).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid

from tpcore.db import build_asyncpg_pool

logger = logging.getLogger("scripts.cleanup_fundamentals_integrity")


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("FAILED — DATABASE_URL not set", file=sys.stderr)
        return 1

    run_id = uuid.uuid4()
    pool = await build_asyncpg_pool(db_url, max_size=2)
    try:
        async with pool.acquire() as conn:
            bad_dates = await conn.fetch(
                """
                SELECT ticker, period_end_date, filing_date
                FROM platform.fundamentals_quarterly
                WHERE period_end_date > filing_date
                ORDER BY filing_date
                """
            )
            bad_shares = await conn.fetch(
                """
                SELECT ticker, period_end_date, shares_outstanding
                FROM platform.fundamentals_quarterly
                WHERE shares_outstanding IS NOT NULL AND shares_outstanding <= 0
                ORDER BY period_end_date
                """
            )
            logger.info("fundamentals cleanup preview:")
            logger.info("  period_end > filing_date rows: %d (to DELETE)", len(bad_dates))
            logger.info("  shares_outstanding <= 0 rows:  %d (to UPDATE to NULL)", len(bad_shares))
            if not bad_dates and not bad_shares:
                logger.info("nothing to clean")
                return 0
            if not args.confirm:
                logger.info("dry-run (use --confirm to apply)")
                return 0

            audit = {
                "deleted_period_end_after_filing": [
                    {"ticker": r["ticker"], "period_end_date": r["period_end_date"].isoformat(), "filing_date": r["filing_date"].isoformat()}
                    for r in bad_dates
                ],
                "updated_shares_to_null": [
                    {"ticker": r["ticker"], "period_end_date": r["period_end_date"].isoformat(), "prior_shares": str(r["shares_outstanding"])}
                    for r in bad_shares
                ],
            }

            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO platform.application_log
                        (engine, run_id, event_type, severity, message, data, recorded_at)
                    VALUES ('ops', $1, 'DATA_CLEANUP', 'WARNING', $2, $3::jsonb, now())
                    """,
                    run_id,
                    f"fundamentals_quarterly: {len(bad_dates)} period-after-filing deleted; {len(bad_shares)} shares set NULL",
                    json.dumps(audit),
                )
                del_result = await conn.execute(
                    """
                    DELETE FROM platform.fundamentals_quarterly
                    WHERE period_end_date > filing_date
                    """
                )
                upd_result = await conn.execute(
                    """
                    UPDATE platform.fundamentals_quarterly
                    SET shares_outstanding = NULL
                    WHERE shares_outstanding IS NOT NULL AND shares_outstanding <= 0
                    """
                )
                logger.info("DELETE: %s", del_result)
                logger.info("UPDATE: %s", upd_result)

            # Post-condition checks.
            rem_dates = await conn.fetchval(
                "SELECT COUNT(*) FROM platform.fundamentals_quarterly WHERE period_end_date > filing_date"
            )
            rem_shares = await conn.fetchval(
                "SELECT COUNT(*) FROM platform.fundamentals_quarterly WHERE shares_outstanding IS NOT NULL AND shares_outstanding <= 0"
            )
            if (rem_dates or 0) > 0 or (rem_shares or 0) > 0:
                logger.error("post-check failed: rem_dates=%s rem_shares=%s", rem_dates, rem_shares)
                return 1
            logger.info("fundamentals integrity clean (run_id=%s)", run_id)
            return 0
    finally:
        await pool.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--confirm", action="store_true", help="actually apply the fixes")
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
