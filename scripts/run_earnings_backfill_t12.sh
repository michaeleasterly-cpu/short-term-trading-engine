#!/usr/bin/env bash
# Backfill platform.earnings_events for the T1+T2 universe from FMP.
# Resolves the "Vector data-blocked" finding in EDGE_VALIDATION_PLAN
# Phase 4: Vector engine needs earnings_events coverage on the active
# universe before its parameter search produces a meaningful result.
#
# Runs ~1,274 FMP earnings fetches with a 0.4s courtesy delay → ~10 min.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

# Resolve T1+T2 tickers from liquidity_tiers, hand them to the
# existing backfill script via --universe.
TICKERS=$(DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -c "
import asyncio, os
from tpcore.db import build_asyncpg_pool
async def main():
    pool = await build_asyncpg_pool(os.environ['DATABASE_URL'])
    async with pool.acquire() as conn:
        rows = await conn.fetch('SELECT ticker FROM platform.liquidity_tiers WHERE tier <= 2 ORDER BY ticker')
    await pool.close()
    print(','.join(r['ticker'] for r in rows))
asyncio.run(main())
")

if [[ -z "$TICKERS" ]]; then
    echo "✗ no T1+T2 tickers in liquidity_tiers — refusing to run"
    exit 1
fi

N_TICKERS=$(echo "$TICKERS" | tr ',' '\n' | wc -l | tr -d ' ')
echo "backfilling earnings_events for $N_TICKERS T1+T2 tickers"

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/backfill_earnings_events.py \
    --universe "$TICKERS" \
    --start 2018-01-01 \
    --end 2026-05-12 \
    "$@"
