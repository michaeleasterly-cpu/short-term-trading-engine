"""Comprehensive audit of every platform table — physical truth +
cross-reference checks. Prints a green/red roll-up per check.
"""
from __future__ import annotations

import asyncio
import os
import sys

from tpcore.db import build_asyncpg_pool


async def main() -> int:
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("FAILED — DATABASE_URL not set", file=sys.stderr)
        return 1
    pool = await build_asyncpg_pool(db_url, max_size=2)
    try:
        async with pool.acquire() as conn:
            async def q(label: str, sql: str) -> None:
                r = await conn.fetchval(sql)
                n = int(r) if r is not None else 0
                tag = "🟢" if n == 0 else "🔴"
                print(f"  {tag} {label:60s} n={n}")

            print("=== catalyst_events ===")
            await q("NULL ticker", "SELECT COUNT(*) FROM platform.catalyst_events WHERE ticker IS NULL")
            await q("NULL event_date", "SELECT COUNT(*) FROM platform.catalyst_events WHERE event_date IS NULL")
            await q("event_date > 365d ahead", "SELECT COUNT(*) FROM platform.catalyst_events WHERE event_date > CURRENT_DATE + INTERVAL '365 days'")
            await q("ticker not in prices_daily", """
                SELECT COUNT(*) FROM platform.catalyst_events ce
                LEFT JOIN (SELECT DISTINCT ticker FROM platform.prices_daily) p ON p.ticker = ce.ticker
                WHERE p.ticker IS NULL
            """)

            print("\n=== liquidity_tiers ===")
            await q("tickers not in prices_daily", """
                SELECT COUNT(*) FROM platform.liquidity_tiers lt
                LEFT JOIN (SELECT DISTINCT ticker FROM platform.prices_daily) p ON p.ticker = lt.ticker
                WHERE p.ticker IS NULL
            """)
            await q("last_updated > 30 days old", "SELECT COUNT(*) FROM platform.liquidity_tiers WHERE last_updated < now() - INTERVAL '30 days'")
            await q("negative median_spread_pct", "SELECT COUNT(*) FROM platform.liquidity_tiers WHERE median_spread_pct < 0")
            await q("negative p95_spread_pct", "SELECT COUNT(*) FROM platform.liquidity_tiers WHERE p95_spread_pct < 0")
            await q("observations <= 0", "SELECT COUNT(*) FROM platform.liquidity_tiers WHERE observations <= 0")

            print("\n=== universe_candidates ===")
            await q("engine NULL", "SELECT COUNT(*) FROM platform.universe_candidates WHERE engine IS NULL")
            await q("as_of_date in future", "SELECT COUNT(*) FROM platform.universe_candidates WHERE as_of_date > CURRENT_DATE")
            await q("last_close <= 0", "SELECT COUNT(*) FROM platform.universe_candidates WHERE last_close IS NOT NULL AND last_close <= 0")
            await q("ticker not in prices_daily", """
                SELECT COUNT(*) FROM platform.universe_candidates uc
                LEFT JOIN (SELECT DISTINCT ticker FROM platform.prices_daily) p ON p.ticker = uc.ticker
                WHERE p.ticker IS NULL
            """)

            print("\n=== spread_observations ===")
            await q("negative spread_pct", "SELECT COUNT(*) FROM platform.spread_observations WHERE spread_pct < 0")
            await q("extreme spread > 50%", "SELECT COUNT(*) FROM platform.spread_observations WHERE spread_pct > 0.5")
            await q("future observed_at", "SELECT COUNT(*) FROM platform.spread_observations WHERE observed_at > now()")

            print("\n=== risk_state ===")
            await q("engine NULL", "SELECT COUNT(*) FROM platform.risk_state WHERE engine IS NULL")
            rows = await conn.fetch("SELECT * FROM platform.risk_state ORDER BY engine")
            for r in rows:
                print(f"  • {dict(r)}")

            print("\n=== corporate_actions cross-ref ===")
            await q("ticker not in prices_daily", """
                SELECT COUNT(*) FROM platform.corporate_actions ca
                LEFT JOIN (SELECT DISTINCT ticker FROM platform.prices_daily) p ON p.ticker = ca.ticker
                WHERE p.ticker IS NULL
            """)

            print("\n=== fundamentals_quarterly cross-ref ===")
            await q("ticker not in prices_daily", """
                SELECT COUNT(*) FROM platform.fundamentals_quarterly fq
                LEFT JOIN (SELECT DISTINCT ticker FROM platform.prices_daily) p ON p.ticker = fq.ticker
                WHERE p.ticker IS NULL
            """)

            print("\n=== tradier_options_chains ===")
            await q("NULL ticker", "SELECT COUNT(*) FROM platform.tradier_options_chains WHERE ticker IS NULL")
            await q("expiration in past", "SELECT COUNT(*) FROM platform.tradier_options_chains WHERE expiration_date < CURRENT_DATE")
            await q("ticker not in prices_daily", """
                SELECT COUNT(*) FROM platform.tradier_options_chains tc
                LEFT JOIN (SELECT DISTINCT ticker FROM platform.prices_daily) p ON p.ticker = tc.ticker
                WHERE p.ticker IS NULL
            """)

            print("\n=== open_orders ===")
            rows = await conn.fetch("SELECT * FROM platform.open_orders")
            for r in rows:
                print(f"  • {dict(r)}")

            print("\n=== ingestion_jobs ===")
            rows = await conn.fetch("SELECT job_name, last_status, last_run_at, last_error FROM platform.ingestion_jobs ORDER BY job_name")
            for r in rows:
                err = (r["last_error"] or "")[:80]
                status = r["last_status"] or "<none>"
                print(f"  • {r['job_name']:30s} status={status:10s} last_run={r['last_run_at']}  err={err}")

    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
