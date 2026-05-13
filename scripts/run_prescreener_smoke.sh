#!/usr/bin/env bash
# Smoke-test the Universe Pre-Screener against the local Supabase DB.
# Runs prescreen_momentum for today's date, prints counters, then shows
# the row count + a few sample rows from platform.universe_candidates.
#
# Idempotent — safe to re-run; rows ON CONFLICT update in place.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -c "
import asyncio, os
from datetime import date
from tpcore.db import build_asyncpg_pool
from tpcore.universe.prescreener import prescreen_momentum

async def main():
    pool = await build_asyncpg_pool(os.environ['DATABASE_URL'])
    try:
        as_of = date.today()
        counters = await prescreen_momentum(pool, as_of)
        print('counters:', counters)
        async with pool.acquire() as conn:
            n = await conn.fetchval(
                'SELECT COUNT(*) FROM platform.universe_candidates '
                'WHERE engine=\$1 AND as_of_date=\$2',
                'momentum', as_of,
            )
            print(f'rows in universe_candidates for momentum/{as_of}: {n}')
            sample = await conn.fetch(
                'SELECT ticker, tier, last_close FROM platform.universe_candidates '
                'WHERE engine=\$1 AND as_of_date=\$2 ORDER BY ticker LIMIT 5',
                'momentum', as_of,
            )
            for r in sample:
                print(f'  {r[\"ticker\"]:8s} tier={r[\"tier\"]} close={r[\"last_close\"]}')
    finally:
        await pool.close()

asyncio.run(main())
"
