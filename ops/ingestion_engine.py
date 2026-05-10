"""Persistent Railway service entry point for the unified ingestion engine.

Unlike the per-cron services this replaces, this process runs forever.
It opens an asyncpg pool, instantiates ``IngestionEngine``, and calls
``run_forever()`` which loops every 60 seconds checking
``platform.ingestion_jobs`` for due rows.

Restart policy on Railway is ``ALWAYS`` — if the container exits for
any reason (DB outage, OOM, etc.) Railway restarts it. Job state lives
in Postgres so a restart resumes cleanly: any job that was 'running'
when the worker died is reclaimed by the staleness clause in
``IngestionEngine._fetch_due`` (30-minute timeout).

Required env:
    DATABASE_URL    — Postgres URL (or DATABASE_URL_IPV4 locally).
    ALPACA_KEY      — for daily_bars + corporate_actions handlers.
    ALPACA_SECRET   — same.
    ALPACA_PAPER    — same.
    FMP_API_KEY     — for fundamentals_refresh handler.
"""
from __future__ import annotations

import asyncio
import os
import sys

import structlog

from tpcore.db import build_asyncpg_pool
from tpcore.ingestion import IngestionEngine

logger = structlog.get_logger(__name__)


async def _amain() -> int:
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    pool = await build_asyncpg_pool(db_url)
    try:
        engine = IngestionEngine(pool)
        sleep_sec = float(os.getenv("INGESTION_TICK_SECONDS", "60"))
        await engine.run_forever(sleep_sec=sleep_sec)
    finally:
        await pool.close()
    return 0  # pragma: no cover - run_forever only exits via cancellation


def main() -> None:  # pragma: no cover - CLI shim
    try:
        raise SystemExit(asyncio.run(_amain()))
    except KeyboardInterrupt:
        logger.info("ingestion.engine.keyboard_interrupt")
        raise SystemExit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
