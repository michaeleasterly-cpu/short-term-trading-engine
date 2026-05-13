#!/usr/bin/env bash
# Run JUST the Universe Pre-Screener stage of the daily update.
# Use when the "Universe (momentum)" row on the dashboard is red but the
# rest of the data is already fresh — re-populating today's
# universe_candidates rows is cheap (~10s) and doesn't need a 30-45min
# full --update.
#
# Detached-friendly: prints structured counters and exits non-zero if
# the kept-count is suspiciously low (<500 = under-populated).
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -c "
import asyncio, os, sys
from datetime import date
from tpcore.db import build_asyncpg_pool
from tpcore.universe.prescreener import prescreen_momentum

async def main():
    pool = await build_asyncpg_pool(os.environ['DATABASE_URL'])
    try:
        counters = await prescreen_momentum(pool, date.today())
        print(f'prescreener counters: {counters}')
        if counters['kept'] < 500:
            print(f'WARN: only {counters[\"kept\"]} candidates kept (expected >=500)', file=sys.stderr)
            return 1
        return 0
    finally:
        await pool.close()

raise SystemExit(asyncio.run(main()))
"
