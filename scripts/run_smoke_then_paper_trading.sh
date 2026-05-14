#!/usr/bin/env bash
# Run pipeline smoke test, then start paper trading ONLY when the smoke
# test actually passed (not just succeeded).
#
# Why the dance: pipeline_smoke_test.py exits 0 on both PASSED and
# SKIPPED (market-closed) paths. A plain `&&` chain would run paper
# trading after a market-closed SKIP — wrong. Solution: check
# platform.application_log for a PIPELINE_SMOKE_PASSED event in the
# last 5 minutes after the smoke run. PASSED is only ever emitted on
# a real end-to-end fill verification.
#
# Trigger: one-shot via launchd at the next NYSE open. See
# ~/Library/LaunchAgents/com.michael.trading.pipeline-smoke-test.plist.
#
# Usage:
#   scripts/run_smoke_then_paper_trading.sh
#
# Logs to ~/Library/Logs/short-term-trading-engine/smoke-then-paper.log
# (via the calling plist's StandardOutPath).
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a
export DATABASE_URL="$DATABASE_URL_IPV4"

echo "════════════════════════════════════════════════════════════════════════"
echo "  SMOKE → PAPER  $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "════════════════════════════════════════════════════════════════════════"

# ── Phase 1 — pipeline smoke test ────────────────────────────────────
echo ""
echo "▶ STEP 1/2  pipeline_smoke_test.py"
echo "────────────────────────────────────────────────────────────────────────"
.venv/bin/python scripts/pipeline_smoke_test.py
SMOKE_RC=$?
if [[ $SMOKE_RC -ne 0 ]]; then
    echo ""
    echo "✗ smoke test exited $SMOKE_RC (FAILED) — paper trading aborted"
    exit "$SMOKE_RC"
fi

# Smoke exit 0 means either PASSED or SKIPPED. Disambiguate via
# application_log: PIPELINE_SMOKE_PASSED is only emitted on PASS.
echo ""
echo "▶ verifying PIPELINE_SMOKE_PASSED event landed (vs SKIPPED)"
echo "────────────────────────────────────────────────────────────────────────"
sleep 5  # allow async log writer to flush

PASS_FLAG=$(.venv/bin/python <<'PY'
import asyncio
import os
from tpcore.db import build_asyncpg_pool


async def main() -> None:
    pool = await build_asyncpg_pool(os.environ["DATABASE_URL"])
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT 1
            FROM platform.application_log
            WHERE event_type = 'PIPELINE_SMOKE_PASSED'
              AND recorded_at > now() - INTERVAL '5 minutes'
            LIMIT 1
            """
        )
    await pool.close()
    print("PASS" if row else "NO")


asyncio.run(main())
PY
)

if [[ "$PASS_FLAG" != "PASS" ]]; then
    echo "✗ no PIPELINE_SMOKE_PASSED event in the last 5 minutes."
    echo "  Most likely cause: smoke test SKIPPED (market closed when launchd fired)."
    echo "  Paper trading aborted (exit 0 — not an error, just a skip)."
    exit 0
fi

# ── Phase 2 — paper trading ──────────────────────────────────────────
echo ""
echo "✓ smoke test PASSED. Proceeding to paper trading."
echo ""
echo "▶ STEP 2/2  start_paper_trading.py"
echo "────────────────────────────────────────────────────────────────────────"
.venv/bin/python scripts/start_paper_trading.py
PAPER_RC=$?
echo ""
echo "════════════════════════════════════════════════════════════════════════"
if [[ $PAPER_RC -eq 0 ]]; then
    echo "  ✓ COMPLETE — smoke PASSED, paper trading dispatched"
else
    echo "  ✗ paper trading exited $PAPER_RC — investigate application_log"
fi
echo "════════════════════════════════════════════════════════════════════════"
exit "$PAPER_RC"
