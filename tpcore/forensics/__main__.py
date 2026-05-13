"""CLI entry point for the Forensics service.

Run from the daily post-close pipeline (or by hand). Reads every engine's
AAR history out of ``platform.aar_events``, detects drawdown periods,
loss clusters, and outlier losses, and INSERTs new triggers into
``platform.forensics_triggers``. Re-running is safe (idempotent via
fingerprint).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import structlog

from tpcore.db import build_asyncpg_pool
from tpcore.forensics.service import ForensicsService


async def amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logger = structlog.get_logger("tpcore.forensics")

    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("Forensics FAILED — DATABASE_URL not set", file=sys.stderr)
        return 1

    # Self-heal: one retry on transient pool-build failure (Supabase
    # pooler can occasionally TCP-RST a fresh connection). The service
    # itself swallows per-engine + per-trigger failures, so we only
    # retry at the pool-creation boundary.
    counts: dict[str, int] = {}
    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            pool = await build_asyncpg_pool(db_url, max_size=2)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning("tpcore.forensics.pool_build_failed", attempt=attempt, error=str(exc))
            await asyncio.sleep(2)
            continue
        try:
            service = ForensicsService(pool=pool)
            counts = await service.run()
            last_error = None
            break
        finally:
            await pool.close()

    if last_error is not None:
        print(f"Forensics FAILED — could not build DB pool after 2 tries: {last_error}", file=sys.stderr)
        return 1

    total = sum(counts.values())
    logger.info("tpcore.forensics.run_complete", counts=counts, total_new=total)
    if total:
        print(f"Forensics: {total} new trigger(s) — {counts}")
    else:
        print("Forensics: no new triggers")
    return 0


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
