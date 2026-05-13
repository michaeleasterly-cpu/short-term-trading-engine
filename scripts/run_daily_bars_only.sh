#!/usr/bin/env bash
# Run JUST today's bar ingestion — no corporate-actions / fundamentals /
# validation stages. Use this when you need fresh prices_daily before a
# scheduler run and don't want to wait for the full ops --update pipeline.
#
# Bypasses scripts/ops.py's per-stage timeout wrapper entirely. Calls the
# underlying tpcore handler directly with no time cap.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -c "
import asyncio, os
from tpcore.db import build_asyncpg_pool
from tpcore.ingestion.handlers import handle_daily_bars

async def main():
    pool = await build_asyncpg_pool(os.environ['DATABASE_URL'])
    try:
        config = {'universe': 'all_active'}
        rows = await handle_daily_bars(pool, config)
        print(f'daily_bars complete: {rows} rows upserted')
    finally:
        await pool.close()

asyncio.run(main())
"
