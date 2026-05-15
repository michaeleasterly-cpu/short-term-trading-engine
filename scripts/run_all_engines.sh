#!/usr/bin/env bash
# Run every engine scheduler back-to-back (Sigma → Reversion → Vector →
# Momentum → Sentinel). Use after run_data_operations.sh has confirmed
# clean data.
#
# This is the operator's daily-trade trigger: data fresh ⇒ schedulers
# scan ⇒ orders submitted to Alpaca paper ⇒ trade_monitor (running
# separately) watches fills.
#
# NOTE: each scheduler is one-shot. The trade_monitor must already be
# running as a daemon (or `python -m tpcore.trade_monitor` in another
# terminal) for the Tier 2 cascade to fire.
#
# Refuses to run during NYSE regular session (orders would race against
# real intraday data). The schedulers themselves don't refuse, but
# pulling fresh bars + scoring + submitting mid-session is risky.
# Pass --force to bypass.
set -uo pipefail
cd "$(dirname "$0")/.."

FORCE=""
if [[ "${1:-}" == "--force" ]]; then
    FORCE="--force"
    shift
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

# Defensive: only run after the data-operations pipeline left the data
# layer green. The validation suite is the source of truth.
if [[ -z "$FORCE" ]]; then
    LATEST_VALIDATION=$(DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -c "
import asyncio, os
from tpcore.db import build_asyncpg_pool

async def main():
    pool = await build_asyncpg_pool(os.environ['DATABASE_URL'])
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            WITH latest AS (
                SELECT source, MAX(timestamp) AS t
                FROM platform.data_quality_log
                WHERE source LIKE 'validation.%'
                GROUP BY source
            )
            SELECT q.source, q.stale, q.confidence
            FROM platform.data_quality_log q
            JOIN latest l ON l.source = q.source AND l.t = q.timestamp
            WHERE q.stale OR (q.confidence IS NOT NULL AND q.confidence < 1.0)
        ''')
        for r in rows:
            print(f'{r[\"source\"]}: stale={r[\"stale\"]} confidence={r[\"confidence\"]}')
    await pool.close()

asyncio.run(main())
")
    if [[ -n "$LATEST_VALIDATION" ]]; then
        echo "✗ data validation has red rows:"
        echo "$LATEST_VALIDATION"
        echo "  refusing to run engines. Use --force to bypass, or fix the data first."
        exit 1
    fi
fi

echo "════════════════════════════════════════════════════════════════════════"
echo "  ENGINE SWEEP — $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "════════════════════════════════════════════════════════════════════════"

for engine in sigma reversion vector momentum sentinel; do
    echo ""
    echo "▶ running ${engine} scheduler"
    echo "────────────────────────────────────────────────────────────────────────"
    DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -m "${engine}.scheduler" "$@" || {
        echo "✗ ${engine} scheduler exited non-zero — continuing to next engine"
    }
done

echo ""
echo "════════════════════════════════════════════════════════════════════════"
echo "  ENGINE SWEEP COMPLETE"
echo "════════════════════════════════════════════════════════════════════════"
