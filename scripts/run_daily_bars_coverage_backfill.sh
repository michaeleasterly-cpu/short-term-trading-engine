#!/usr/bin/env bash
# One-shot remediation: refill the 2026-05-11→14 daily_bars coverage hole.
# Full active universe, end_offset_days=1 (market-hours-safe — pulls
# through yesterday, never hits the SIP end=today 403). ON CONFLICT DO
# NOTHING so it only fills holes, idempotent.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
# shellcheck disable=SC2046
export $(grep -E '^DATABASE_URL_IPV4=|^ALPACA_' .env | xargs)
export DATABASE_URL="${DATABASE_URL_IPV4:?DATABASE_URL_IPV4 missing}"
"$REPO_ROOT/.venv/bin/python" - <<'PY'
import asyncio, os
from tpcore.db import build_asyncpg_pool
from tpcore.ingestion.handlers import handle_daily_bars

async def main():
    pool = await build_asyncpg_pool(os.environ["DATABASE_URL"])
    try:
        # lookback 10d covers 05-05..05-14; end_offset_days=1 ends at
        # yesterday so it's safe to run any time of day.
        rows = await handle_daily_bars(pool, {
            "universe": "active",
            "lookback_days": 10,
            "end_offset_days": 1,
        })
        print(f"daily_bars coverage backfill: {rows} rows upserted")
        async with pool.acquire() as c:
            for d in ("2026-05-11","2026-05-12","2026-05-13","2026-05-14"):
                n = await c.fetchval(
                    "SELECT COUNT(DISTINCT ticker) FROM platform.prices_daily WHERE date=$1", d)
                print(f"  {d}: {n:,} tickers")
    finally:
        await pool.close()

asyncio.run(main())
PY
