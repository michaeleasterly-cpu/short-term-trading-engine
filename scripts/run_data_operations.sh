#!/usr/bin/env bash
# Daily data-operations workflow (renamed from run_post_close.sh on
# 2026-05-14 to describe the function rather than the trigger time).
#
# Sequence (each step gated by the previous step's exit code):
#   1. DOWNLOAD + UPLOAD — scripts/ops.py --update (15 stages, all sources;
#                          final stage `forensics` scans aar_events for
#                          drawdown / loss-cluster / outlier-loss triggers
#                          and writes Sprint Dossiers under docs/sprints/).
#   2. VERIFY            — scripts/run_audit_all_tables.sh
#   3. VERIFY            — scripts/run_stage.sh data_validation
#   4. SELF-HEAL         — re-validate; if a daily-bars completeness/
#                          freshness check is red, the pipeline runs the
#                          canonical parameterised backfill ITSELF and
#                          re-validates, up to MAX_HEAL_ATTEMPTS. Reds a
#                          bars backfill can't fix escalate immediately.
#                          NEVER reaches Step 6 unless 100% green.
#  4b. MATVIEW           — refresh platform.prices_daily_tickers.
#  4c. DEEP AUDIT        — scripts/audit_pipeline.py run unattended every
#                          cycle; known_knowns 🔴 → alarm + hard stop
#                          (no emit). Advisory yellows are non-gating.
#   5. COMPRESS          — scripts/run_compress_backfill_csvs.sh (any
#                          uncompressed CSVs left under data/*_backfill/)
#   6. EMIT EVENT        — writes DATA_OPERATIONS_COMPLETE to
#                          platform.application_log; the engine-service
#                          daemon (installed via install_all_daemons.sh)
#                          picks it up and fires scripts/run_all_engines.sh.
#                          Replaces the old inline engine sweep on
#                          2026-05-14 to decouple data-ops from execution.
#
# Refuses to run during NYSE regular session (ops.py --update enforces this).
# Pass --force to bypass the market-closed check.
#
# Usage:
#   scripts/run_data_operations.sh             # the canonical daily workflow
#   scripts/run_data_operations.sh --force     # bypass market-closed guard
set -uo pipefail
cd "$(dirname "$0")/.."

# ── Self-exclusion lock (2026-05-15) ────────────────────────────────────
# The auto-heal loop (Step 4) makes this workflow's runtime variable —
# a multi-retry daily_bars backfill can extend a normally-short run by
# many minutes. launchd fires this daily at 05:30 local; the lock
# guarantees a slow run can never overlap the next scheduled fire (or a
# manual invocation), so two data-ops pipelines can't pounce on the same
# tables / DB pool concurrently. `mkdir` is atomic; a stale lock whose
# PID is dead is reclaimed. Lock dir is released by the EXIT trap below.
LOCK_DIR="${TMPDIR:-/tmp}/ste-data-operations.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    _lock_pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
    if [[ -n "$_lock_pid" ]] && kill -0 "$_lock_pid" 2>/dev/null; then
        echo "✗ data-operations already running (pid $_lock_pid) — refusing to"
        echo "  start a concurrent run. This is the self-exclusion guard; if the"
        echo "  prior run is genuinely stuck, kill pid $_lock_pid then remove"
        echo "  $LOCK_DIR."
        if command -v osascript >/dev/null 2>&1; then
            osascript -e "display notification \"data_operations skipped — prior run (pid $_lock_pid) still active\" with title \"STE — data_operations OVERLAP\" sound name \"Basso\"" 2>/dev/null || true
        fi
        exit 0
    fi
    echo "  (reclaiming stale lock — pid '${_lock_pid:-?}' not alive)"
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR" 2>/dev/null || { echo "✗ cannot acquire lock dir"; exit 1; }
fi
echo "$$" > "$LOCK_DIR/pid"

FORCE_FLAG=""
if [[ "${1:-}" == "--force" ]]; then
    FORCE_FLAG="--force"
fi

# Generate one run_id for the whole workflow (added 2026-05-15 to close
# the daemon-progress visibility gap). Passed to ops.py via --run-id so
# every event — the 15 --update stages AND the bash-wrapper steps below
# — shares one row family in application_log. The progress panel
# queries by run_id, so this is what makes wrapper steps show up.
RUN_ID=$(.venv/bin/python -c "import uuid; print(uuid.uuid4())")

# Helper: emit a single application_log event with the shared RUN_ID.
# Swallows logging failures (a missing event must not crash the
# wrapper). Use as:
#   _log_event INGESTION_START wrapper_audit ["optional message"]
_log_event() {
    local event_type="$1"
    local stage_name="$2"
    local msg="${3:-}"
    DATABASE_URL="${DATABASE_URL_IPV4:-$DATABASE_URL}" \
        .venv/bin/python scripts/_log_event.py \
            --run-id "$RUN_ID" \
            --event-type "$event_type" \
            --stage-name "$stage_name" \
            ${msg:+--message "$msg"} \
            2>/dev/null || true
}

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
trap '_rc=$?; rm -rf "$LOCK_DIR" 2>/dev/null; if [[ $_rc -ne 0 ]]; then _notify_failure "trap (unexpected)" $_rc; fi' EXIT

echo "════════════════════════════════════════════════════════════════════════"
echo "  DATA OPERATIONS WORKFLOW — $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "════════════════════════════════════════════════════════════════════════"

# Step 1+2 — download + upload via the 7-stage --update pipeline.
echo ""
echo "▶ STEP 1+2 / 6  download + upload  (ops.py --update)"
echo "────────────────────────────────────────────────────────────────────────"
set -a
# shellcheck disable=SC1091
source .env
set +a
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/ops.py \
    --update --source data_operations_daemon --run-id "$RUN_ID" $FORCE_FLAG
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
echo "▶ STEP 3 / 6  verify cross-table integrity"
echo "────────────────────────────────────────────────────────────────────────"
_log_event INGESTION_START wrapper_audit
scripts/run_audit_all_tables.sh
AUDIT_RC=$?
if [[ $AUDIT_RC -ne 0 ]]; then
    _log_event INGESTION_FAILED wrapper_audit "audit exited $AUDIT_RC"
    echo "✗ audit_all_tables exited $AUDIT_RC"
    _notify_failure "audit_all_tables" $AUDIT_RC
    exit $AUDIT_RC
fi
_log_event INGESTION_COMPLETE wrapper_audit

# Step 4 — AUTONOMOUS SELF-HEAL (rebuilt 2026-05-15).
#
# Old behaviour: validation red → notify + exit. That made every gap a
# babysitting event. New behaviour: validation red on a daily-bars
# completeness/freshness check → the pipeline *fixes it itself* via the
# canonical parameterised backfill, re-validates, and only escalates to
# the operator after MAX_HEAL_ATTEMPTS failed auto-fixes. Reds that a
# bars backfill genuinely cannot fix (fundamentals/corp-actions/etc.)
# escalate immediately — pretending to heal those would be dishonest
# and would let the system trade on bad data.
#
# Safety invariant: DATA_OPERATIONS_COMPLETE (Step 6) is NEVER emitted
# unless validation is fully green. If auto-heal exhausts its attempts,
# this script exits non-zero WITHOUT emitting — so the engine-service
# daemon never fires the engine sweep on unhealed data. "100% data or
# don't trade" is enforced structurally, not by a human watching.
echo ""
echo "▶ STEP 4 / 6  autonomous self-heal — guarantee 100% validation green"
echo "────────────────────────────────────────────────────────────────────────"
_log_event INGESTION_START wrapper_validation_recheck

MAX_HEAL_ATTEMPTS=3
# The heal uses the BOUNDED targeted gap-repair
# (`--param repair_gaps=true`): the stage re-pulls only the tickers the
# completeness invariant currently flags, over a window bracketing the
# oldest missing session. A whole-universe `force_refresh` was proven
# 2026-05-15 to exceed the 3600s stage timeout (two 60-min timeouts),
# so it could never actually self-heal. Targeted repair is a handful of
# tickers → seconds, well inside the timeout and the pre-open window.

# Print the bare source name of every currently-red validation check
# (one per line). Reads the freshest row per source from
# data_quality_log — written by the validation stage of ops.py --update
# and re-written by each `ops.py --stage data_validation` re-run below.
_red_validation_sources() {
    DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -c "
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
            SELECT q.source
            FROM platform.data_quality_log q
            JOIN latest l ON l.source = q.source AND l.t = q.timestamp
            WHERE q.stale OR (q.confidence IS NOT NULL AND q.confidence < 1.0)
            ORDER BY q.source
        ''')
        for r in rows:
            print(r['source'])
    await pool.close()
asyncio.run(main())
"
}

heal_attempt=0
while :; do
    RED_SOURCES="$(_red_validation_sources)"
    if [[ -z "$RED_SOURCES" ]]; then
        break   # fully green — invariant satisfied
    fi

    # Split red into the auto-healable class (daily-bars completeness /
    # freshness — a backfill can fill these) vs everything else.
    NON_HEALABLE="$(grep -v '^validation\.prices_daily_\(completeness\|freshness\)$' <<<"$RED_SOURCES" || true)"
    if [[ -n "$NON_HEALABLE" ]]; then
        _log_event INGESTION_FAILED wrapper_validation_recheck "non-healable validation red"
        echo "✗ validation red on check(s) a daily_bars backfill cannot fix:"
        echo "$NON_HEALABLE" | sed 's/^/    /'
        echo ""
        echo "  These require operator investigation (fundamentals / corp-"
        echo "  actions / classifications / etc. — not a prices gap). Not"
        echo "  emitting DATA_OPERATIONS_COMPLETE; engines will NOT trade."
        _notify_failure "validation (non-healable: $(echo "$NON_HEALABLE" | tr '\n' ',' | sed 's/,$//'))" 1
        exit 1
    fi

    if (( heal_attempt >= MAX_HEAL_ATTEMPTS )); then
        _log_event INGESTION_FAILED wrapper_validation_recheck "auto-heal exhausted after $MAX_HEAL_ATTEMPTS attempts"
        echo "✗ daily-bars validation STILL red after $MAX_HEAL_ATTEMPTS auto-heal"
        echo "  backfill attempts:"
        echo "$RED_SOURCES" | sed 's/^/    /'
        echo ""
        echo "  The backfill is not closing the gap — likely an upstream"
        echo "  vendor outage (Alpaca) or a structural data issue. Escalating."
        echo "  NOT emitting DATA_OPERATIONS_COMPLETE; engines will NOT trade"
        echo "  on incomplete data."
        _notify_failure "auto-heal exhausted ($MAX_HEAL_ATTEMPTS attempts, still red)" 1
        exit 1
    fi

    heal_attempt=$((heal_attempt + 1))
    echo ""
    echo "  ⟳ auto-heal attempt ${heal_attempt}/${MAX_HEAL_ATTEMPTS} — red:"
    echo "$RED_SOURCES" | sed 's/^/      /'
    echo "    running canonical BOUNDED targeted gap-repair (no one-off script):"
    echo "    ops.py --stage daily_bars --param repair_gaps=true --force"
    echo "    (re-pulls only the invariant-flagged tickers — seconds, not 60+ min)"
    _log_event INGESTION_START wrapper_autoheal "attempt ${heal_attempt}/${MAX_HEAL_ATTEMPTS}"

    DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/ops.py \
        --stage daily_bars \
        --param repair_gaps=true \
        --force --run-id "$RUN_ID"
    HEAL_RC=$?
    if [[ $HEAL_RC -ne 0 ]]; then
        _log_event INGESTION_FAILED wrapper_autoheal "backfill exited $HEAL_RC (attempt ${heal_attempt})"
        echo "✗ auto-heal backfill itself exited $HEAL_RC on attempt ${heal_attempt}."
        echo "  Cannot self-heal through a failing backfill. Escalating; NOT"
        echo "  emitting DATA_OPERATIONS_COMPLETE."
        _notify_failure "auto-heal backfill (attempt ${heal_attempt}, rc $HEAL_RC)" $HEAL_RC
        exit $HEAL_RC
    fi

    # Re-validate so data_quality_log reflects the post-backfill state;
    # the loop then re-probes and either breaks green or retries.
    echo "    re-validating after backfill..."
    DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/ops.py \
        --stage data_validation --run-id "$RUN_ID"
    REVAL_RC=$?
    if [[ $REVAL_RC -ne 0 ]]; then
        _log_event INGESTION_FAILED wrapper_autoheal "re-validation exited $REVAL_RC"
        echo "✗ re-validation stage exited $REVAL_RC after backfill. Escalating."
        _notify_failure "auto-heal re-validation (rc $REVAL_RC)" $REVAL_RC
        exit $REVAL_RC
    fi
    _log_event INGESTION_COMPLETE wrapper_autoheal "attempt ${heal_attempt} re-validated"
done

_log_event INGESTION_COMPLETE wrapper_validation_recheck
if (( heal_attempt > 0 )); then
    echo "✓ validation fully green after ${heal_attempt} autonomous heal attempt(s) — zero gaps, no human needed"
else
    echo "✓ validation fully green on first pass — zero gaps"
fi

# Step 4b — refresh dashboard matview now that prices_daily is current.
# REFRESH CONCURRENTLY so dashboard reads don't block while it runs (~1s).
echo ""
echo "▶ STEP 4b / 6  refresh platform.prices_daily_tickers matview"
echo "────────────────────────────────────────────────────────────────────────"
_log_event INGESTION_START wrapper_matview_refresh
if DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -c "
import asyncio, asyncpg, os
async def main():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'])
    await conn.execute('REFRESH MATERIALIZED VIEW CONCURRENTLY platform.prices_daily_tickers')
    print('✓ prices_daily_tickers refreshed')
    await conn.close()
asyncio.run(main())
"; then
    _log_event INGESTION_COMPLETE wrapper_matview_refresh
else
    _log_event INGESTION_FAILED wrapper_matview_refresh "matview refresh non-fatal"
    echo "  (matview refresh failed — non-fatal, dashboard will see stale ticker list)"
fi

# Step 4c — 4-phase deep audit, run UNATTENDED (added 2026-05-15).
#
# Closes the "audit is theatre" gap: audit_pipeline.py used to be
# on-demand only, so heuristic drift accumulated silently between
# operator asks. It now runs every data-ops cycle, serialized inline
# here (NOT a separate launchd job — a concurrent job is exactly what
# would pounce on the engine sweep / allocator). It persists every
# finding to data_quality_log (dashboard-visible) regardless of outcome.
#
# Gating policy: default invocation exits 1 ONLY on a known_knowns FAIL
# — a hard, named, actionable red. We do NOT pass --strict: heuristic
# WARN yellows (known_unknowns / unknown_* phases) are advisory and must
# not block trading; the zero-tolerance completeness invariant in Step 4
# is the hard data gate. A known_knowns 🔴 → alarm + exit WITHOUT
# emitting DATA_OPERATIONS_COMPLETE (engines do not trade through a
# confirmed structural red).
echo ""
echo "▶ STEP 4c / 6  4-phase deep audit (unattended)"
echo "────────────────────────────────────────────────────────────────────────"
_log_event INGESTION_START wrapper_deep_audit
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/audit_pipeline.py
DEEP_AUDIT_RC=$?
if [[ $DEEP_AUDIT_RC -eq 1 ]]; then
    _log_event INGESTION_FAILED wrapper_deep_audit "known_knowns FAIL (exit 1)"
    echo "✗ 4-phase audit found a known_knowns FAIL (🔴) — a hard, named"
    echo "  structural red. Findings persisted to data_quality_log (see the"
    echo "  dashboard audit panel). NOT emitting DATA_OPERATIONS_COMPLETE;"
    echo "  engines will NOT trade through a confirmed red."
    _notify_failure "4-phase deep audit (known_knowns 🔴)" 1
    exit 1
elif [[ $DEEP_AUDIT_RC -ne 0 ]]; then
    # Any other non-zero (e.g. 2 only if --strict, or an unexpected
    # crash) is treated conservatively as a hard stop.
    _log_event INGESTION_FAILED wrapper_deep_audit "audit exited $DEEP_AUDIT_RC"
    echo "✗ 4-phase audit exited $DEEP_AUDIT_RC (unexpected). Escalating; NOT"
    echo "  emitting DATA_OPERATIONS_COMPLETE."
    _notify_failure "4-phase deep audit (rc $DEEP_AUDIT_RC)" $DEEP_AUDIT_RC
    exit $DEEP_AUDIT_RC
fi
_log_event INGESTION_COMPLETE wrapper_deep_audit
echo "✓ 4-phase audit clean (no known_knowns 🔴; advisory yellows are non-gating)"

# Step 5 — compress any CSVs left behind by the backfill scripts.
echo ""
echo "▶ STEP 5 / 6  compress backfill CSVs"
echo "────────────────────────────────────────────────────────────────────────"
_log_event INGESTION_START wrapper_compress
if scripts/run_compress_backfill_csvs.sh; then
    _log_event INGESTION_COMPLETE wrapper_compress
else
    _log_event INGESTION_FAILED wrapper_compress "compress exited non-zero"
fi

# Step 6 — emit DATA_OPERATIONS_COMPLETE so the engine-service daemon
# fires scripts/run_all_engines.sh. Replaces the inline engine sweep
# on 2026-05-14 to decouple data-ops latency / failures from engine
# execution. Set SKIP_ENGINES=1 to skip the emission (data-only run).
if [[ "${SKIP_ENGINES:-0}" == "1" ]]; then
    echo ""
    echo "▶ STEP 6 / 6  emit DATA_OPERATIONS_COMPLETE — SKIPPED (SKIP_ENGINES=1)"
    _log_event INGESTION_START wrapper_emit_event "skipped via SKIP_ENGINES=1"
    _log_event INGESTION_COMPLETE wrapper_emit_event "skipped via SKIP_ENGINES=1"
else
    echo ""
    echo "▶ STEP 6 / 6  emit DATA_OPERATIONS_COMPLETE → engine-service daemon"
    echo "────────────────────────────────────────────────────────────────────────"
    _log_event INGESTION_START wrapper_emit_event
    DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -c "
import asyncio, asyncpg, os, uuid
async def main():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'])
    await conn.execute(
        '''
        INSERT INTO platform.application_log
            (engine, run_id, event_type, severity, message, data)
        VALUES (\$1, \$2, \$3, \$4, \$5, NULL)
        ''',
        'ops', uuid.uuid4(), 'DATA_OPERATIONS_COMPLETE', 'INFO',
        'data-operations workflow finished — triggering engine sweep',
    )
    await conn.close()
    print('✓ DATA_OPERATIONS_COMPLETE written — engine-service daemon will pick it up within 60s')
asyncio.run(main())
" || {
        EMIT_RC=$?
        _log_event INGESTION_FAILED wrapper_emit_event "emit exited $EMIT_RC"
        echo "✗ failed to emit DATA_OPERATIONS_COMPLETE (exit $EMIT_RC) — engines will NOT run."
        echo "  Investigate: is application_log reachable? Is the daemon installed?"
        _notify_failure "emit DATA_OPERATIONS_COMPLETE" $EMIT_RC
        exit $EMIT_RC
    }
    _log_event INGESTION_COMPLETE wrapper_emit_event
fi

# Forensics now runs as the final stage of `ops.py --update` (Step 1
# above) — registered in scripts/ops.py:_STAGE_SPECS as the `forensics`
# stage. The prior standalone Step 7 (`python -m tpcore.forensics`)
# was removed 2026-05-15 to keep the maintenance surface uniform.

# Disable the failure-trap before exiting cleanly so the success path
# doesn't fire a spurious "trap (unexpected)" notification.
trap - EXIT
echo ""
echo "════════════════════════════════════════════════════════════════════════"
echo "  DATA OPERATIONS COMPLETE — every check 🟢"
echo "════════════════════════════════════════════════════════════════════════"
