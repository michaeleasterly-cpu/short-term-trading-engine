#!/usr/bin/env python3
"""Pre-wipe logical snapshot of the to-be-TRUNCATEd ticker graph (Plan 2).

Supabase PITR (automatic 7-day) is the platform-level rollback; this is the
belt-and-suspenders logical copy of the exact pre-wipe state (the agent has no
Supabase management-API credential, so a platform on-demand backup is dashboard-
only — this CSV dump via the Postgres connection is the achievable equivalent).
Output is gitignored. Run: .venv/bin/python scripts/rebuild_snapshot_ticker_graph.py
"""
from __future__ import annotations

import asyncio
import csv
import os
from pathlib import Path

from dotenv import load_dotenv

# The exact TRUNCATE set (Plan 2 Task 7). Largest first so a failure surfaces fast.
TABLES = [
    "prices_daily", "fundamentals_quarterly", "prices_daily_staging",
    "ticker_classifications", "ticker_history", "issuers", "issuer_securities",
    "issuer_history", "corporate_events", "corporate_actions", "earnings_events",
    "short_interest", "borrow_rates", "insider_transactions", "insider_sentiment",
    "social_sentiment", "sec_material_events", "spread_observations",
    "liquidity_tiers", "universe_candidates", "aar_events", "ticker_lifecycle_events",
]
OUT_DIR = Path("data/rebuild_2026-06-04/ticker_graph_snapshot")


async def _main() -> None:
    load_dotenv("/Users/michael/short-term-trading-engine/.env")
    url = os.environ.get("DATABASE_URL_IPV4") or os.environ["DATABASE_URL"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    from tpcore.db import build_asyncpg_pool

    pool = await build_asyncpg_pool(url, max_size=2, timeout=900.0)
    try:
        async with pool.acquire() as conn:
            for table in TABLES:
                live = await conn.fetchval(f"SELECT count(*) FROM platform.{table}")
                dest = OUT_DIR / f"{table}.csv"
                await conn.copy_from_query(
                    f"SELECT * FROM platform.{table}",
                    output=str(dest),
                    format="csv",
                    header=True,
                )
                with dest.open(newline="") as fh:
                    n = sum(1 for _ in csv.reader(fh)) - 1
                mb = dest.stat().st_size / 1e6
                status = "OK" if n == live else "MISMATCH"
                print(f"  {table:<24} {n:>9,} rows  {mb:>8.1f} MB  [{status}]")
                if status == "MISMATCH":
                    raise SystemExit(f"row-count mismatch {table}: csv={n} live={live}")
    finally:
        await pool.close()
    total = sum(f.stat().st_size for f in OUT_DIR.glob("*.csv")) / 1e6
    print(f"Ticker-graph snapshot complete: {OUT_DIR} ({total:.0f} MB total)")


if __name__ == "__main__":
    asyncio.run(_main())
