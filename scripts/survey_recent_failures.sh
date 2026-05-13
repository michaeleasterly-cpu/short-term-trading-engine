#!/usr/bin/env bash
# Survey recent failures in application_log + data_quality_log so we can
# build a self-healing layer grounded in the actual error population.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -c "
import asyncio, os, json
from tpcore.db import build_asyncpg_pool

async def main():
    pool = await build_asyncpg_pool(os.environ['DATABASE_URL'])
    async with pool.acquire() as conn:
        print('=== INGESTION_FAILED events (last 14 days, grouped) ===')
        rows = await conn.fetch('''
            SELECT engine,
                   data->>'stage' AS stage,
                   COALESCE(data->>'reason', data->>'exception_type', 'unknown') AS reason,
                   COUNT(*) AS n,
                   MAX(recorded_at) AS latest
            FROM platform.application_log
            WHERE event_type = 'INGESTION_FAILED'
              AND recorded_at > now() - INTERVAL '14 days'
            GROUP BY engine, stage, reason
            ORDER BY n DESC
        ''')
        for r in rows:
            print(f'  {r[\"engine\"]:8s} {(r[\"stage\"] or \"-\"):24s} {(r[\"reason\"] or \"-\"):25s} n={r[\"n\"]:3d}  latest={r[\"latest\"]}')

        print()
        print('=== ERROR severity events (last 14 days, sample messages) ===')
        rows = await conn.fetch('''
            SELECT engine, event_type, message, recorded_at
            FROM platform.application_log
            WHERE severity = 'ERROR'
              AND recorded_at > now() - INTERVAL '14 days'
            ORDER BY recorded_at DESC
            LIMIT 15
        ''')
        for r in rows:
            msg = (r['message'] or '')[:120]
            print(f'  {r[\"recorded_at\"].strftime(\"%Y-%m-%d %H:%M\")} {r[\"engine\"]:8s} {r[\"event_type\"]:20s} {msg}')

        print()
        print('=== data_quality_log validation failures (last 14 days) ===')
        rows = await conn.fetch('''
            SELECT source, stale, confidence, latency_ms, timestamp, notes
            FROM platform.data_quality_log
            WHERE source LIKE 'validation.%' AND (stale OR confidence < 1.0)
              AND timestamp > now() - INTERVAL '14 days'
            ORDER BY timestamp DESC
            LIMIT 20
        ''')
        for r in rows:
            notes = (r['notes'] or '')[:120].replace('\n',' ')
            print(f'  {r[\"timestamp\"].strftime(\"%Y-%m-%d %H:%M\")} {r[\"source\"]:30s} stale={r[\"stale\"]} conf={r[\"confidence\"]} {notes}')
    await pool.close()

asyncio.run(main())
"
