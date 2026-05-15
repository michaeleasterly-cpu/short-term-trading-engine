"""One-shot baseline dump for the FULL-SNAPSHOT ingestion sources.

The CSV-first retrofit defends against vendor-side data truncation
(FRED BAMLH0A0HYM2, 2026-05-15) by archiving every ingest before the
DB upsert, then comparing each archive's row count to its predecessor
(``tpcore.ingestion.csv_archive.detect_shrinkage``).

That comparison is only meaningful for sources whose handler
re-pulls the **entire history every run** — a sudden row-count drop
then unambiguously means the vendor truncated. Two of the five
retrofitted sources are full-snapshot:

* ``fred_macro``               — FREDAdapter re-pulls 1996→now each run.
* ``alpaca_corporate_actions`` — re-fetches 2018→now each run.

The other three (``alpaca_daily_bars``, ``fmp_fundamentals``,
``fmp_catalyst_events``) archive only the **incremental slice** touched
that run. A full-table baseline would be the wrong comparator for them
— the next incremental run would look like a 99% "shrinkage" and fire
a false alarm. Their shrinkage detection works naturally run-over-run
once two real ingests exist; no baseline is needed (and a full one
would poison it). This script therefore ONLY seeds the two
full-snapshot sources.

Idempotent: re-running writes a fresh dated baseline.

Run::

    DATABASE_URL=$DATABASE_URL_IPV4 python scripts/dump_baseline_archives.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime
from typing import Any

import structlog

from tpcore.db import build_asyncpg_pool
from tpcore.ingestion.csv_archive import write_archive

logger = structlog.get_logger(__name__)


# Full-snapshot sources ONLY — (source, table, fieldnames, ORDER BY).
FULL_SNAPSHOT_SOURCES: tuple[tuple[str, str, list[str], str], ...] = (
    (
        "fred_macro",
        "platform.macro_indicators",
        ["indicator", "date", "value", "recorded_at"],
        "indicator, date",
    ),
    (
        "alpaca_corporate_actions",
        "platform.corporate_actions",
        ["ticker", "action_date", "action_type", "ratio", "raw_data", "recorded_at"],
        "ticker, action_date",
    ),
)


async def _dump_one(pool, source: str, table: str, fieldnames: list[str], order_by: str) -> int:
    sql = f"SELECT {', '.join(fieldnames)} FROM {table} ORDER BY {order_by}"
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
    archive_rows: list[dict[str, Any]] = [
        {k: ("" if v is None else str(v)) for k, v in dict(r).items()}
        for r in rows
    ]
    result = write_archive(source, archive_rows, fieldnames=fieldnames)
    print(f"  {source:<28}: {result.rows_written:,} rows → {result.path.name}")
    return result.rows_written


async def amain() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 1
    pool = await build_asyncpg_pool(db_url)
    started = datetime.now(UTC)
    try:
        print(f"Baseline archive dump (full-snapshot sources) — {started:%Y-%m-%d %H:%M:%S UTC}")
        total = 0
        for source, table, fields, order_by in FULL_SNAPSHOT_SOURCES:
            try:
                total += await _dump_one(pool, source, table, fields, order_by)
            except Exception as exc:  # noqa: BLE001
                print(f"  {source:<28}: FAILED — {exc}", file=sys.stderr)
        elapsed = (datetime.now(UTC) - started).total_seconds()
        print(f"\ntotal rows archived: {total:,}  ({elapsed:.1f}s)")
        print("\nNote: alpaca_daily_bars / fmp_fundamentals / fmp_catalyst_events")
        print("are incremental-archive sources — no baseline by design (a full")
        print("baseline would false-flag their next incremental run as shrinkage).")
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(amain()))
