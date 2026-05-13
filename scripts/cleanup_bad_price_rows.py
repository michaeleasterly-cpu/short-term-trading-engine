"""One-shot cleanup for structurally bad rows in ``platform.prices_daily``.

Same predicate as ``tpcore.quality.validation.checks.row_integrity``:
deletes rows where ``close <= 0``, ``high < low``, NULLs in price/volume
columns, or future dates. Each deletion is logged to
``platform.application_log`` with a structured payload so the audit
trail is queryable forever.

Usage::

    scripts/run_cleanup_bad_price_rows.sh           # dry-run (default)
    scripts/run_cleanup_bad_price_rows.sh --confirm  # actually delete

Why not an Alembic migration? Alembic is for schema, not data. A data
fix written as a migration would re-run on every fresh DB bootstrap,
which is the opposite of what we want — the bad rows are a one-time
ingestion artifact from the deprecated Tradier source, and the
underlying ingest path has changed since.
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
from tpcore.quality.validation.checks.row_integrity import _INTEGRITY_PREDICATE

logger = logging.getLogger("scripts.cleanup_bad_price_rows")


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
            rows = await conn.fetch(
                f"""
                SELECT ticker, date, close, high, low, volume, delisted, source
                FROM platform.prices_daily
                WHERE {_INTEGRITY_PREDICATE}
                ORDER BY date, ticker
                """
            )
            if not rows:
                logger.info("cleanup: no bad rows found — nothing to do")
                return 0

            logger.info("cleanup: %d row(s) match the integrity predicate", len(rows))
            for r in rows:
                logger.info(
                    "  %-8s %s  close=%s  hi=%s  lo=%s  vol=%s  source=%s",
                    r["ticker"], r["date"], r["close"], r["high"],
                    r["low"], r["volume"], r["source"],
                )

            if not args.confirm:
                logger.info("cleanup: dry-run (use --confirm to delete)")
                return 0

            # Snapshot of what we're deleting goes in the audit log payload.
            audit_payload = [
                {
                    "ticker": r["ticker"],
                    "date": r["date"].isoformat(),
                    "close": str(r["close"]) if r["close"] is not None else None,
                    "high": str(r["high"]) if r["high"] is not None else None,
                    "low": str(r["low"]) if r["low"] is not None else None,
                    "volume": int(r["volume"]) if r["volume"] is not None else None,
                    "source": r["source"],
                }
                for r in rows
            ]

            async with conn.transaction():
                # Pre-write the audit row so we have a record even if the
                # DELETE itself were to fail or be interrupted.
                await conn.execute(
                    """
                    INSERT INTO platform.application_log
                        (engine, run_id, event_type, severity, message, data, recorded_at)
                    VALUES ('ops', $1, 'DATA_CLEANUP', 'WARNING', $2, $3::jsonb, now())
                    """,
                    run_id,
                    f"deleting {len(rows)} structurally-bad prices_daily rows",
                    json.dumps({"deleted_rows": audit_payload}),
                )
                deleted = await conn.execute(
                    f"DELETE FROM platform.prices_daily WHERE {_INTEGRITY_PREDICATE}"
                )
                logger.info("cleanup: deletion result: %s", deleted)

            # Post-condition: predicate should return zero rows now.
            remaining = await conn.fetchval(
                f"SELECT COUNT(*) FROM platform.prices_daily WHERE {_INTEGRITY_PREDICATE}"
            )
            if remaining and int(remaining) > 0:
                logger.error("cleanup: %d row(s) still match predicate post-delete", remaining)
                return 1
            logger.info("cleanup: integrity predicate is now clean (0 rows remaining); run_id=%s", run_id)
            return 0
    finally:
        await pool.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--confirm",
        action="store_true",
        help="actually perform the DELETE; omitted = dry-run preview only",
    )
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
