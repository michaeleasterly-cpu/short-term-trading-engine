"""One-shot backfill — SH, PSQ, GLD daily bars (Sentinel basket gap).

The Sentinel basket spec (SH 35% / PSQ 25% / TLT 20% / GLD 10% / SQQQ
10%) has historically been missing three tickers from
``platform.prices_daily``. The engine renormalized to TLT + SQQQ as a
documented workaround; this script closes the gap so the full basket
trades natively.

Approach: re-use the existing ``handle_daily_bars`` handler with an
explicit ticker list and a 6,000-day lookback (~2010-01-01 → today,
matching the master plan's "earliest available" target). The handler
itself handles pagination, rate-limit pauses, and idempotent upsert
into ``platform.prices_daily`` — no new ingestion logic.

After this script:
* Run ``python scripts/ops.py --stage tier_refresh --force`` to classify
  SH/PSQ/GLD into their liquidity tiers.
* Re-run ``scripts/run_sentinel_backtest.sh`` to confirm the full basket
  is traded without renormalization.

Idempotent — re-running upserts existing rows in place.
"""
from __future__ import annotations

import asyncio
import os
import sys

from tpcore.db import build_asyncpg_pool
from tpcore.ingestion.handlers import handle_daily_bars

SENTINEL_BACKFILL_TICKERS: tuple[str, ...] = ("SH", "PSQ", "GLD")
LOOKBACK_DAYS = 6000  # ~2010-01-01 → today; Alpaca SIP free tier covers this window


async def amain() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 1
    pool = await build_asyncpg_pool(db_url)
    try:
        # ``end_offset_days=1`` skips "today" so a mid-session backfill
        # doesn't 403 against Alpaca's SIP free tier (which requires an
        # intraday-data subscription for end=today during regular hours).
        # The after-hours scheduled refresh leaves this at 0.
        rows = await handle_daily_bars(
            pool,
            {
                "universe": list(SENTINEL_BACKFILL_TICKERS),
                "lookback_days": LOOKBACK_DAYS,
                "end_offset_days": 1,
            },
        )
        print(f"backfill complete — rows upserted: {rows}")
        async with pool.acquire() as conn:
            for ticker in SENTINEL_BACKFILL_TICKERS:
                summary = await conn.fetchrow(
                    """
                    SELECT MIN(date) AS mn, MAX(date) AS mx, COUNT(*) AS n
                    FROM platform.prices_daily WHERE ticker = $1
                    """,
                    ticker,
                )
                print(
                    f"  {ticker:>4}: n={summary['n']:>5}  "
                    f"{summary['mn']} → {summary['mx']}"
                )
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(amain()))
