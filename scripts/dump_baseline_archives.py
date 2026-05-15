"""One-shot baseline archive dump for the DB-baselineable sources.

The CSV-first retrofit defends against vendor-side data truncation
(FRED BAMLH0A0HYM2, 2026-05-15) by archiving every ingest before the
DB upsert. Two distinct guarantees ride on the archive directory:

1. **Shrinkage detection** — only the *full-snapshot* sources
   (``fred_macro``, ``alpaca_corporate_actions``) re-pull their entire
   history every run, so ``detect_shrinkage`` comparing the new
   archive's row count to its predecessor unambiguously catches a
   vendor truncation. Shrinkage detection is wired ONLY for these two
   (see the handlers — daily_bars/fundamentals/catalyst do not call
   ``detect_shrinkage``).

2. **Presence** — the ``csv_archive_presence`` audit check requires
   *an* archive on disk for every retrofitted source. ``fmp_fundamentals``
   and ``fmp_catalyst_events`` archive only the incremental slice each
   run, so they have no archive until their handler next fires — which
   leaves ``csv_archive_presence`` WARN-yellow indefinitely. Seeding a
   one-time baseline from current DB state satisfies presence and is
   completely safe: there is NO shrinkage comparator on these sources
   to "poison" (point 1). The earlier "no baseline by design" reasoning
   conflated the two guarantees — corrected 2026-05-15.

This script therefore seeds every source that can be dumped from the
DB: the 2 shrinkage sources + the 2 presence-only incremental sources.
``alpaca_daily_bars`` is excluded — ~20M rows make a one-shot dump
impractical, and it self-archives on every handler run (so its
presence is satisfied by the normal/parameterised stage run, not a
baseline).

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


# Every DB-baselineable source — (source, table, fieldnames, ORDER BY).
# First two: full-snapshot, feed shrinkage detection. Last two:
# presence-only incremental sources (no shrinkage comparator to poison).
# alpaca_daily_bars excluded (size + self-archives every handler run).
BASELINE_SOURCES: tuple[tuple[str, str, list[str], str], ...] = (
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
    (
        "fmp_fundamentals",
        "platform.fundamentals_quarterly",
        [
            "ticker", "filing_date", "period_end_date", "period_label",
            "net_income", "fcf", "operating_cash_flow", "capex", "revenue",
            "total_assets", "total_liabilities", "current_assets",
            "current_liabilities", "receivables", "cash_and_equivalents",
            "shares_outstanding", "pb", "de", "recorded_at",
        ],
        "ticker, period_end_date",
    ),
    (
        "fmp_catalyst_events",
        "platform.catalyst_events",
        ["ticker", "event_date", "event_type", "magnitude_pct", "source", "recorded_at"],
        "ticker, event_date",
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
        print(f"Baseline archive dump — {started:%Y-%m-%d %H:%M:%S UTC}")
        total = 0
        for source, table, fields, order_by in BASELINE_SOURCES:
            try:
                total += await _dump_one(pool, source, table, fields, order_by)
            except Exception as exc:  # noqa: BLE001
                print(f"  {source:<28}: FAILED — {exc}", file=sys.stderr)
        elapsed = (datetime.now(UTC) - started).total_seconds()
        print(f"\ntotal rows archived: {total:,}  ({elapsed:.1f}s)")
        print("\nNote: alpaca_daily_bars is excluded (~20M rows; it self-archives")
        print("on every handler run, so csv_archive_presence is satisfied by the")
        print("normal/parameterised daily_bars stage, not a baseline dump).")
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(amain()))
