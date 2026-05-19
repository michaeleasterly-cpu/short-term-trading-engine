#!/usr/bin/env bash
# Bounded targeted repair of the 2026-05-15 missing tail.
#
# Root cause (diagnosed, not guessed): the full force_refresh re-pull
# timed out at 3600s, cut off in the alphabetical T-Z tail (747
# tickers that had a 2026-05-14 bar but no 2026-05-15 bar; positional,
# not random — un-attempted, not failed/absent). repair_gaps is blind
# to a freshness coverage_collapse, and a full re-pull re-times-out.
# Re-pull ONLY the missing set via the canonical daily_bars explicit
# universe (CSV) path — ~8 chunks, minutes, no timeout.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

MISSING="$(DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python - <<'PY'
import asyncio, os
e={}
for l in open(".env"):
    l=l.strip()
    if l and not l.startswith("#") and "=" in l:
        k,v=l.split("=",1); e[k]=v.strip().strip('"').strip("'")
import asyncpg
async def m():
    # statement_cache_size/jit: keep in sync with tpcore.db.build_asyncpg_pool (Supabase pooler safety)
    c=await asyncpg.connect(e.get("DATABASE_URL_IPV4") or e["DATABASE_URL"], timeout=30, statement_cache_size=0, server_settings={"jit": "off"})
    try:
        rows=await c.fetch("""
          SELECT p14.ticker FROM
            (SELECT DISTINCT ticker FROM platform.prices_daily
             WHERE date='2026-05-14' AND delisted=false) p14
          LEFT JOIN
            (SELECT DISTINCT ticker FROM platform.prices_daily
             WHERE date='2026-05-15') p15 ON p14.ticker=p15.ticker
          WHERE p15.ticker IS NULL ORDER BY p14.ticker""")
        print(",".join(r["ticker"] for r in rows))
    finally:
        await c.close()
asyncio.run(m())
PY
)"

N=$(awk -F, '{print NF}' <<<"$MISSING")
echo "missing tail: $N tickers"
[ -z "$MISSING" ] && { echo "nothing missing — already complete"; exit 0; }

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -u scripts/ops.py \
  --stage daily_bars --param universe="$MISSING" \
  --param force_refresh=true --param end_offset_days=1 --param lookback_days=4
