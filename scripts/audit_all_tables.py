"""Comprehensive audit of every platform table.

Thin caller: the structured cross-table violation checks now live in
``tpcore.audit.cross_table`` (the SoT, persisted to data_quality_log
so the auditheal loop can act on them). This script preserves the
operator-facing stdout roll-up and the informational dump sections
(risk_state / open_orders).

Exit code is intentionally still 0 on violations in this phase — the
honest gate flip is wired in a later phase via
``python -m tpcore.auditheal`` (isolated, independently reviewable).
A crash / missing DSN still exits 1.
"""
from __future__ import annotations

import asyncio
import os
import sys

from tpcore.audit.cross_table import run_cross_table_audit
from tpcore.db import build_asyncpg_pool


async def main() -> int:
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("FAILED — DATABASE_URL not set", file=sys.stderr)
        return 1
    pool = await build_asyncpg_pool(db_url, max_size=2)
    try:
        findings = await run_cross_table_audit(pool, persist=True)
        last_table = None
        for f in findings:
            if f.table != last_table:
                print(f"\n=== {f.table} ===")
                last_table = f.table
            tag = "🟢" if f.severity == "OK" else "🔴"
            print(f"  {tag} {f.check_name:40s} n={f.count}")

        async with pool.acquire() as conn:
            print("\n=== risk_state (dump) ===")
            for r in await conn.fetch(
                "SELECT * FROM platform.risk_state ORDER BY engine"
            ):
                print(f"  • {dict(r)}")
            print("\n=== open_orders (dump) ===")
            for r in await conn.fetch("SELECT * FROM platform.open_orders"):
                print(f"  • {dict(r)}")

        n_red = sum(1 for f in findings if f.severity != "OK")
        print(f"\nTOTAL cross-table checks={len(findings)}  🔴 {n_red}")
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
