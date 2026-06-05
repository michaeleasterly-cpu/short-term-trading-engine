#!/usr/bin/env python3
"""Phase-1 snapshot of the PRESERVE-class ops tables before the Plan 2 cutover.

These tables are EXCLUDED from the Task-7 TRUNCATE, but a verbatim off-DB CSV
copy is the belt-and-suspenders rollback (the SACRED-carve-out analog for
operational state). asyncpg-based because this environment has no ``psql``;
``DATABASE_URL_IPV4`` is the same DSN Alembic uses for DDL, so it is
session-capable. Run: ``.venv/bin/python scripts/rebuild_snapshot_preserve_tables.py``
"""
from __future__ import annotations

import asyncio
import csv
import os
from pathlib import Path

from dotenv import load_dotenv

PRESERVE_TABLES = ("ingest_manifest", "allocations", "risk_close_ledger")
OUT_DIR = Path("data/rebuild_2026-06-04/preserve")


async def _main() -> None:
    load_dotenv()
    url = os.environ.get("DATABASE_URL_IPV4") or os.environ["DATABASE_URL"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    from tpcore.db import build_asyncpg_pool

    pool = await build_asyncpg_pool(url, max_size=2, timeout=30.0)
    try:
        async with pool.acquire() as conn:
            for table in PRESERVE_TABLES:
                dest = OUT_DIR / f"{table}.csv"
                await conn.copy_from_query(
                    f"SELECT * FROM platform.{table}",
                    output=str(dest),
                    format="csv",
                    header=True,
                )
                live = await conn.fetchval(f"SELECT count(*) FROM platform.{table}")
                with dest.open(newline="") as fh:  # csv.reader handles multiline fields
                    lines = sum(1 for _ in csv.reader(fh)) - 1  # minus header
                status = "OK" if lines == live else "MISMATCH"
                print(f"snapshot: {table} -> {dest} ({lines} rows, live={live}) [{status}]")
                if status == "MISMATCH":
                    raise SystemExit(f"row-count mismatch for {table}: csv={lines} live={live}")
    finally:
        await pool.close()
    print(f"Phase-1 PRESERVE snapshot complete: {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(_main())
