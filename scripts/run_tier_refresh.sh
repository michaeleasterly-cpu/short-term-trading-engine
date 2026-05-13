#!/usr/bin/env bash
# Quarterly liquidity-tier refresh.
#
# Two-stage pipeline:
#   1. Re-run the Corwin-Schultz bootstrap against the last ~35 days of
#      prices_daily; writes per-ticker spread observations to
#      platform.spread_observations.
#   2. Re-aggregate to platform.liquidity_tiers (per-ticker tier T1-T5
#      from the median observed spread).
#
# Run quarterly OR whenever the universe has materially changed
# (post Phase 1 universe expansion, post ingestion of a new exchange,
# after re-ingesting historical data, etc.). Cost model depends on the
# tiers being current.
#
# Long-running (~20-30 min depending on universe size). Foreground run
# is fine; not gated by a timeout wrapper.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

echo "════════════════════════════════════════════════════════════════════════"
echo "  STAGE 1 / 2 — Corwin-Schultz spread bootstrap"
echo "════════════════════════════════════════════════════════════════════════"
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -c "
import asyncio, os
from tpcore.db import build_asyncpg_pool
from tpcore.backtest.spread_estimator import rank_universe_by_liquidity

async def main():
    pool = await build_asyncpg_pool(os.environ['DATABASE_URL'])
    try:
        # coarse_filter=False matches the post-2026-05-12 bootstrap that
        # accepted mega-caps (IEX volume is ~5% of NBBO, so the >1M volume
        # filter excluded MSFT/META/etc.). Cleared by feedback memory.
        results = await rank_universe_by_liquidity(pool, persist=True, coarse_filter=False)
        print(f'  → wrote {len(results)} spread observations to platform.spread_observations')
    finally:
        await pool.close()

asyncio.run(main())
" || { echo "STAGE 1 FAILED — aborting"; exit 1; }

echo ""
echo "════════════════════════════════════════════════════════════════════════"
echo "  STAGE 2 / 2 — re-aggregate liquidity_tiers"
echo "════════════════════════════════════════════════════════════════════════"
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/assign_liquidity_tiers.py \
  || { echo "STAGE 2 FAILED"; exit 1; }

echo ""
echo "════════════════════════════════════════════════════════════════════════"
echo "  TIER REFRESH COMPLETE"
echo "════════════════════════════════════════════════════════════════════════"
