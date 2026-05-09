"""Railway cron entry point for the Data Validation Suite.

Wires the suite to the same operational pattern as `sigma/scheduler.py` and
`reversion/scheduler.py`:

* loads ``DATABASE_URL`` (or ``DATABASE_URL_IPV4`` for the local Supabase
  pooler) from the env;
* opens an asyncpg pool, runs `run_suite`, prints the report;
* pings ``HEALTHCHECKS_VALIDATION_URL`` (success URL on pass, ``/fail`` on
  failure or unhandled exception);
* exits 0 on pass, 1 on fail.

Cron schedule: weekly, Sunday 06:00 UTC (see ``railway.json``). Exit cleanly
— Railway's cron workers expect a single-shot process.
"""
from __future__ import annotations

import asyncio
import os
import sys

import structlog

from tpcore.db import build_asyncpg_pool
from tpcore.quality.validation.cli import format_report
from tpcore.quality.validation.suite import run_suite

logger = structlog.get_logger(__name__)

_HEALTHCHECKS_ENV = "HEALTHCHECKS_VALIDATION_URL"


async def _ping_healthcheck(suffix: str = "") -> None:
    """Best-effort ping. Failure here never affects the validation outcome."""
    url = os.getenv(_HEALTHCHECKS_ENV)
    if not url:
        return
    import httpx

    target = url.rstrip("/") + suffix
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.get(target)
    except Exception as exc:  # pragma: no cover - network-best-effort
        logger.warning(
            "validation.healthcheck.ping_failed", suffix=suffix, error=str(exc)
        )


async def _amain() -> int:
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    await _ping_healthcheck("/start")
    try:
        pool = await build_asyncpg_pool(db_url)
        try:
            result = await run_suite(pool)
        finally:
            await pool.close()
    except Exception as exc:
        logger.exception("validation.cron.run_failed", error=str(exc))
        await _ping_healthcheck("/fail")
        return 1

    print(format_report(result))
    if result.passed:
        await _ping_healthcheck("")  # success
        return 0
    await _ping_healthcheck("/fail")
    return 1


def main() -> None:  # pragma: no cover - CLI shim
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
