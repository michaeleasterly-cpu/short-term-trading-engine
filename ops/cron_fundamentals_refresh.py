"""Railway cron entry point for the weekly fundamentals cache refresh.

Mirrors ``ops/cron_validation.py``: opens an asyncpg pool, runs the
refresh, exits 0/1. Fundamentals freshness is best-effort — a missed
refresh degrades gracefully (the cache miss path falls back to FMP on
demand, so engines still work, just with extra FMP calls). Run health
is observable via `platform.application_log` like every other cron.

What it does
------------
1. Build an asyncpg pool from ``DATABASE_URL`` (or ``DATABASE_URL_IPV4``
   locally for the Supabase pooler).
2. Build ``FMPFundamentalsAdapter`` from ``FMP_API_KEY`` and wrap it in
   ``FundamentalsCache(pool, adapter)``.
3. Call ``cache.backfill_all(tickers=None)`` — backfills every distinct
   ticker active in ``platform.prices_daily`` over the last 90 days.

Cron schedule: weekly, Sunday 03:00 UTC (see ``railway.json``). Runs an
hour before the corporate-actions ingest so engines starting Monday
morning have both the freshest filings *and* the freshest split
adjustments.

Exit codes
----------
* 0 — refresh completed; zero per-symbol failures.
* 1 — at least one per-symbol failure, or unhandled error.
* 2 — config error (missing DATABASE_URL or FMP_API_KEY).
"""
from __future__ import annotations

import asyncio
import os
import sys

import structlog

from tpcore.db import build_asyncpg_pool
from tpcore.fmp import FMPFundamentalsAdapter
from tpcore.fundamentals.cache import FundamentalsCache
from tpcore.outage import DataProviderOutage

logger = structlog.get_logger(__name__)


async def _amain() -> int:
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    if not os.getenv("FMP_API_KEY"):
        print("FMP_API_KEY not set", file=sys.stderr)
        return 2

    try:
        pool = await build_asyncpg_pool(db_url)
    except Exception as exc:
        logger.exception("fundamentals.cron.pool_failed", error=str(exc))
        return 1

    try:
        try:
            adapter = FMPFundamentalsAdapter()
        except DataProviderOutage as exc:
            logger.error("fundamentals.cron.adapter_unavailable", error=str(exc))
            return 1

        try:
            cache = FundamentalsCache(pool, adapter=adapter)
            logger.info("fundamentals.cron.start")
            total, no_data, failures, skipped = await cache.backfill_all(tickers=None)
            logger.info(
                "fundamentals.cron.done",
                rows_upserted=total,
                no_data=len(no_data),
                failures=len(failures),
                skipped_fresh=skipped,
            )
            print(
                f"fundamentals refresh complete  "
                f"rows_upserted={total}  "
                f"no_data={len(no_data)}  "
                f"failures={len(failures)}  "
                f"skipped_fresh={skipped}"
            )
            for sym, why in no_data:
                print(f"  skip  {sym}: {why}")
            for sym, why in failures:
                print(f"  fail  {sym}: {why}")
            return 0 if not failures else 1
        finally:
            await adapter.aclose()
    except Exception as exc:  # pragma: no cover - last-resort guard
        logger.exception("fundamentals.cron.unhandled", error=str(exc))
        return 1
    finally:
        await pool.close()


def main() -> None:  # pragma: no cover - CLI shim
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
