#!/usr/bin/env bash
# Time every query in _fetch_platform_health() individually to find the
# slow one. The panel is reportedly taking minutes — figure out why.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -c "
import asyncio, os, time
from tpcore.db import build_asyncpg_pool

async def time_query(conn, name, sql, *args):
    t0 = time.monotonic()
    r = await conn.fetch(sql, *args) if sql.strip().lower().startswith(('select','with')) else await conn.fetchval(sql, *args)
    elapsed = time.monotonic() - t0
    n = len(r) if isinstance(r, list) else 1
    print(f'  {name:40s} {elapsed*1000:8.1f} ms ({n} row(s))')

async def main():
    t_pool = time.monotonic()
    pool = await build_asyncpg_pool(os.environ['DATABASE_URL'], max_size=2)
    print(f'pool open: {(time.monotonic()-t_pool)*1000:.1f} ms')
    try:
        async with pool.acquire() as conn:
            await time_query(conn, 'bars (MAX+COUNT DISTINCT FILTER)', '''
                SELECT MAX(date) AS latest_date,
                       COUNT(DISTINCT ticker) FILTER (
                           WHERE date >= CURRENT_DATE - INTERVAL '5 days'
                       ) AS recent_tickers
                FROM platform.prices_daily
            ''')
            await time_query(conn, 'bars (MAX only)', 'SELECT MAX(date) FROM platform.prices_daily')
            await time_query(conn, 'bars (COUNT DISTINCT, last 5d)', '''
                SELECT COUNT(DISTINCT ticker) FROM platform.prices_daily WHERE date >= CURRENT_DATE - INTERVAL '5 days'
            ''')
            await time_query(conn, 'fundamentals (MAX recorded_at)', 'SELECT MAX(recorded_at), MAX(period_end_date) FROM platform.fundamentals_quarterly')
            await time_query(conn, 'corp_actions (MAX recorded_at)', 'SELECT MAX(recorded_at) FROM platform.corporate_actions')
            await time_query(conn, 'universe (today count)', '''
                SELECT MAX(as_of_date) AS latest_date,
                       COUNT(*) FILTER (WHERE as_of_date = CURRENT_DATE) AS today_count
                FROM platform.universe_candidates WHERE engine = \$1
            ''', 'momentum')
            await time_query(conn, 'application_log (last STARTUP run)', '''
                SELECT run_id, MAX(recorded_at) AS started_at
                FROM platform.application_log
                WHERE engine = \$1 AND event_type = \$2
                GROUP BY run_id ORDER BY started_at DESC LIMIT 1
            ''', 'ops', 'STARTUP')
            await time_query(conn, 'data_quality_log (validation 7d)', '''
                SELECT source, MAX(timestamp), SUM(CASE WHEN stale OR confidence < 1.0 THEN 1 ELSE 0 END), COUNT(*)
                FROM platform.data_quality_log
                WHERE source LIKE \$1 AND timestamp > now() - INTERVAL '7 days'
                GROUP BY source
            ''', 'validation.%')
    finally:
        await pool.close()

asyncio.run(main())
"
