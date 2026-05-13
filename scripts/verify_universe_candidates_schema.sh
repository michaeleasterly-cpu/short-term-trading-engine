#!/usr/bin/env bash
# Quick check that platform.universe_candidates exists with expected columns.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -c "
import asyncio, os
from tpcore.db import build_asyncpg_pool

async def main():
    pool = await build_asyncpg_pool(os.environ['DATABASE_URL'])
    async with pool.acquire() as conn:
        cols = await conn.fetch('''
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'platform' AND table_name = 'universe_candidates'
            ORDER BY ordinal_position
        ''')
        for c in cols:
            print(f'  {c[\"column_name\"]:14s} {c[\"data_type\"]:25s} nullable={c[\"is_nullable\"]}')
        idx = await conn.fetch('''
            SELECT indexname FROM pg_indexes
            WHERE schemaname='platform' AND tablename='universe_candidates'
        ''')
        print('indexes:', [r['indexname'] for r in idx])
    await pool.close()

asyncio.run(main())
"
