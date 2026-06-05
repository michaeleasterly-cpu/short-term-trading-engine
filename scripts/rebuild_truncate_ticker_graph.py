#!/usr/bin/env python3
"""Plan 2 Task 7 — the irreversible ticker-graph wipe (asyncpg; no psql here).

One TRUNCATE statement so the mutual classification_id FKs are satisfiable.
EXCLUDES macro_data + the PRESERVE-class ops tables (ingest_manifest, allocations,
risk_close_ledger). Built-in FK-completeness pre-check (Task 6): every child of a
truncated parent must itself be in the list, or Postgres rejects the TRUNCATE.

PRECONDITIONS (operator-gated): PRESERVE snapshot taken; Supabase on-demand
snapshot + PITR anchor recorded; writers paused; migrations 0300->0500 applied
(alembic current == 20260604_0500); explicit operator GO.

Run: REBUILD_WIPE_CONFIRM=I_HAVE_THE_SNAPSHOT_AND_OPERATOR_GO \\
       .venv/bin/python scripts/rebuild_truncate_ticker_graph.py
"""
from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

# FK-complete (live FK map 2026-06-04 + ticker_lifecycle_events kept, fold deferred
# to Plan 3). options_max_pain EXCLUDED — dropped by 0300 before this runs.
TRUNCATE_TABLES = [
    "prices_daily", "prices_daily_staging", "fundamentals_quarterly",
    "ticker_classifications", "ticker_history", "issuers", "issuer_securities",
    "issuer_history", "corporate_events", "corporate_actions", "earnings_events",
    "short_interest", "borrow_rates", "insider_transactions", "insider_sentiment",
    "social_sentiment", "sec_material_events", "spread_observations",
    "liquidity_tiers", "universe_candidates", "aar_events", "ticker_lifecycle_events",
]
_CONFIRM = "I_HAVE_THE_SNAPSHOT_AND_OPERATOR_GO"


async def _main() -> None:
    if os.environ.get("REBUILD_WIPE_CONFIRM") != _CONFIRM:
        raise SystemExit(f"Refusing: set REBUILD_WIPE_CONFIRM={_CONFIRM}")
    load_dotenv()
    url = os.environ.get("DATABASE_URL_IPV4") or os.environ["DATABASE_URL"]
    from tpcore.db import build_asyncpg_pool

    pool = await build_asyncpg_pool(url, max_size=2, timeout=120.0)
    try:
        async with pool.acquire() as conn:
            # FK-completeness gate: any child of a to-be-truncated parent that is
            # NOT in the list would make TRUNCATE fail — surface it loudly first.
            gaps = await conn.fetch(
                """
                SELECT DISTINCT c.conrelid::regclass::text AS child,
                                c.confrelid::regclass::text AS parent
                FROM pg_constraint c
                WHERE c.contype = 'f' AND c.connamespace = 'platform'::regnamespace
                  AND split_part(c.confrelid::regclass::text, '.', 2) = ANY($1::text[])
                  AND split_part(c.conrelid::regclass::text, '.', 2) <> ALL($1::text[])
                """,
                TRUNCATE_TABLES,
            )
            missing = sorted({r["child"] for r in gaps})
            if missing:
                raise SystemExit(
                    "FK-INCOMPLETE: these children FK a truncated parent but are not in "
                    f"the TRUNCATE list (add them or the wipe fails): {missing}"
                )
            qualified = ", ".join(f"platform.{t}" for t in TRUNCATE_TABLES)
            await conn.execute(f"TRUNCATE TABLE {qualified} RESTART IDENTITY")
    finally:
        await pool.close()
    print(f"WIPE complete — truncated {len(TRUNCATE_TABLES)} tables.")


if __name__ == "__main__":
    asyncio.run(_main())
