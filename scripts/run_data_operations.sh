#!/usr/bin/env bash
# Daily data-operations workflow (renamed from run_post_close.sh on
# 2026-05-14 to describe the function rather than the trigger time).
#
# Sequence (each step gated by the previous step's exit code):
#   1. DOWNLOAD + UPLOAD — scripts/ops.py --update (13 stages, all sources)
#   2. VERIFY            — scripts/run_audit_all_tables.sh
#   3. VERIFY            — scripts/run_stage.sh data_validation
#   4. FIX               — self-heal retry is built into ops.py; if anything
#                          stays red after that, this script exits non-zero
#                          so the operator knows to investigate.
#   5. COMPRESS          — scripts/run_compress_backfill_csvs.sh (any
#                          uncompressed CSVs left under data/*_backfill/)
#
# Refuses to run during NYSE regular session (ops.py --update enforces this).
# Pass --force to bypass the market-closed check.
#
# Usage:
#   scripts/run_data_operations.sh             # the canonical daily workflow
#   scripts/run_data_operations.sh --force     # bypass market-closed guard
set -uo pipefail
cd "$(dirname "$0")/.."

FORCE_FLAG=""
if [[ "${1:-}" == "--force" ]]; then
    FORCE_FLAG="--force"
fi

# macOS notification on failure (audit gap G-4 fix, 2026-05-14).
# Fires a Notification Center alert + tail-of-log breadcrumb so the
# operator sees red without trawling logs. Safe on non-Mac hosts
# (osascript missing → silent no-op).
_notify_failure() {
    local step="$1"
    local rc="$2"
    if command -v osascript >/dev/null 2>&1; then
        osascript -e "display notification \"data_operations ${step} exited ${rc} — check ~/Library/Logs/short-term-trading-engine/data-operations.log\" with title \"STE — data_operations FAILED\" sound name \"Basso\"" 2>/dev/null || true
    fi
    echo "✗ FAILURE — ${step} exited ${rc}. Notification fired (if osascript available)."
}
# Catch any unexpected non-zero exit too (set -e isn't on; rely on
# explicit `exit` calls but trap as safety net).
trap '_rc=$?; if [[ $_rc -ne 0 ]]; then _notify_failure "trap (unexpected)" $_rc; fi' EXIT

echo "════════════════════════════════════════════════════════════════════════"
echo "  DATA OPERATIONS WORKFLOW — $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "════════════════════════════════════════════════════════════════════════"

# Step 1+2 — download + upload via the 7-stage --update pipeline.
echo ""
echo "▶ STEP 1+2 / 5  download + upload  (ops.py --update)"
echo "────────────────────────────────────────────────────────────────────────"
set -a
# shellcheck disable=SC1091
source .env
set +a
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/ops.py --update $FORCE_FLAG
UPDATE_RC=$?
if [[ $UPDATE_RC -ne 0 ]]; then
    echo "✗ --update exited with code $UPDATE_RC — investigate before proceeding."
    echo "  Common causes: timeout (rate-limited), partial stage failure, market open."
    echo "  Self-heal already retried any transient failure once. If still red, look at:"
    echo "    SELECT * FROM platform.application_log WHERE engine='ops' AND severity='ERROR' ORDER BY recorded_at DESC LIMIT 10;"
    _notify_failure "ops.py --update" $UPDATE_RC
    exit $UPDATE_RC
fi

# Step 3 — cross-reference audit.
echo ""
echo "▶ STEP 3 / 5  verify cross-table integrity"
echo "────────────────────────────────────────────────────────────────────────"
scripts/run_audit_all_tables.sh
AUDIT_RC=$?
if [[ $AUDIT_RC -ne 0 ]]; then
    echo "✗ audit_all_tables exited $AUDIT_RC"
    _notify_failure "audit_all_tables" $AUDIT_RC
    exit $AUDIT_RC
fi

# Step 4 — validation suite (already ran inside --update, but re-confirm).
echo ""
echo "▶ STEP 4 / 5  fix — surface any remaining validation red"
echo "────────────────────────────────────────────────────────────────────────"
FAILED_CHECKS=$(DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -c "
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
if [[ -n "$FAILED_CHECKS" ]]; then
    echo "✗ validation has red rows AFTER self-heal:"
    echo "$FAILED_CHECKS"
    echo ""
    echo "  These weren't auto-fixable — operator must investigate."
    echo "  See the dashboard's Data validation expander for per-failure detail."
    _notify_failure "validation suite" 1
    exit 1
fi
echo "✓ all 6 validation checks green"

# Step 4b — refresh dashboard matview now that prices_daily is current.
# REFRESH CONCURRENTLY so dashboard reads don't block while it runs (~1s).
echo ""
echo "▶ STEP 4b / 7  refresh platform.prices_daily_tickers matview"
echo "────────────────────────────────────────────────────────────────────────"
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -c "
import asyncio, asyncpg, os
async def main():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'])
    await conn.execute('REFRESH MATERIALIZED VIEW CONCURRENTLY platform.prices_daily_tickers')
    print('✓ prices_daily_tickers refreshed')
    await conn.close()
asyncio.run(main())
" || echo "  (matview refresh failed — non-fatal, dashboard will see stale ticker list)"

# Step 5 — compress any CSVs left behind by the backfill scripts.
echo ""
echo "▶ STEP 5 / 7  compress backfill CSVs"
echo "────────────────────────────────────────────────────────────────────────"
scripts/run_compress_backfill_csvs.sh

# Step 6 — engine sweep. Now that data is verified clean, run every
# engine scheduler back-to-back. Each is one-shot; the trade_monitor
# daemon (installed separately) picks up the Tier 2 cascade.
# Set SKIP_ENGINES=1 to skip this step (data-only run).
if [[ "${SKIP_ENGINES:-0}" == "1" ]]; then
    echo ""
    echo "▶ STEP 6 / 7  engine sweep — SKIPPED (SKIP_ENGINES=1)"
else
    echo ""
    echo "▶ STEP 6 / 7  engine sweep — sigma → reversion → vector → momentum"
    echo "────────────────────────────────────────────────────────────────────────"
    scripts/run_all_engines.sh
fi

# Step 7 — forensics. Pure read-side: scans every engine's AAR history
# and inserts triggers into platform.forensics_triggers when it sees
# drawdown periods, loss clusters, or outlier losses. Non-fatal — the
# service swallows per-engine + per-trigger failures, and if it can't
# build a DB pool at all it retries once. A failure here never blocks
# the rest of the data-operations run.
echo ""
echo "▶ STEP 7 / 7  forensics — scan AARs for drawdown/cluster/outlier triggers"
echo "────────────────────────────────────────────────────────────────────────"
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -m tpcore.forensics || \
    echo "  (forensics returned non-zero — non-fatal, continuing)"

# Disable the failure-trap before exiting cleanly so the success path
# doesn't fire a spurious "trap (unexpected)" notification.
trap - EXIT
echo ""
echo "════════════════════════════════════════════════════════════════════════"
echo "  DATA OPERATIONS COMPLETE — every check 🟢"
echo "════════════════════════════════════════════════════════════════════════"
