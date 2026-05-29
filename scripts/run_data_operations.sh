#!/usr/bin/env bash
# Daily data-operations workflow (renamed from run_post_close.sh on
# 2026-05-14 to describe the function rather than the trigger time).
#
# Sequence (each step gated by the previous step's exit code):
#   1. DOWNLOAD + UPLOAD — scripts/ops.py --update (15 stages, all sources;
#                          final stage `forensics` scans aar_events for
#                          drawdown / loss-cluster / outlier-loss triggers
#                          and writes Sprint Dossiers under docs/sprints/).
#   2. VERIFY            — tpcore.auditheal (cross-table audit + auto-remediation)
#   3. VERIFY            — scripts/run_stage.sh data_validation
#   4. SELF-HEAL         — re-validate; if a daily-bars completeness/
#                          freshness check is red, the pipeline runs the
#                          canonical parameterised backfill ITSELF and
#                          re-validates, up to MAX_HEAL_ATTEMPTS. Reds a
#                          bars backfill can't fix escalate immediately.
#                          NEVER reaches Step 6 unless 100% green.
#  4b. MATVIEW           — refresh platform.prices_daily_tickers.
#  4c. DEEP AUDIT        — scripts/audit_data_pipeline.py run unattended every
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

# ── Resolve DATABASE_URL early (Mac vs Railway) ─────────────────────────
# Mac sources .env (operator's IPv4 string lives there); Railway injects
# DATABASE_URL (IPv6 endpoint) directly into the container env and has
# NO .env file on disk. Prior `source .env` blew up under `set -u` when
# the file was absent; the hardcoded `"$DATABASE_URL_IPV4"` references
# below also failed if that var wasn't set. After this block,
# `DATABASE_URL_IPV4` is guaranteed set to the right DSN for the
# environment, so every downstream `DATABASE_URL="$DATABASE_URL_IPV4"`
# invocation just works.
if [[ -n "${RAILWAY_ENVIRONMENT:-}" ]]; then
    : "${DATABASE_URL:=${DATABASE_URL_IPV6:-${DATABASE_URL_IPV4:-}}}"
    DATABASE_URL_IPV4="${DATABASE_URL}"
else
    set -a
    # shellcheck disable=SC1091
    [[ -f .env ]] && source .env
    set +a
    : "${DATABASE_URL_IPV4:=${DATABASE_URL:-}}"
    : "${DATABASE_URL:=${DATABASE_URL_IPV4:-}}"
fi
if [[ -z "${DATABASE_URL_IPV4:-}" ]]; then
    echo "✗ no DATABASE URL resolved — set DATABASE_URL or DATABASE_URL_IPV4/_IPV6"
    exit 1
fi
export DATABASE_URL_IPV4 DATABASE_URL

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

# Resolve the Python interpreter once for the whole script. Mac uses
# the project-local .venv/bin/python; Railway's railpack builder
# lands the venv at /app/.deps/bin/python (per railway.json
# _build_note). The heartbeat function used to discover this locally
# but the script's main invocations hardcoded the venv path, which
# broke on Railway (2026-05-28: "scripts/run_data_operations.sh: line
# 250: .venv/bin/python: No such file or directory"). Hoist the
# discovery to the top so every callsite shares the resolved path.
_MAC_VENV_PY=".venv/bin/python"
_RAILWAY_VENV_PY="/app/.deps/bin/python"
if [[ -x "$_MAC_VENV_PY" ]]; then PY="$_MAC_VENV_PY"
elif [[ -x "$_RAILWAY_VENV_PY" ]]; then PY="$_RAILWAY_VENV_PY"
elif command -v python3 >/dev/null 2>&1; then PY="$(command -v python3)"
else
    echo "✗ no python found (tried .venv, /app/.deps, python3) — cannot continue" >&2
    exit 127
fi

# Generate one run_id for the whole workflow (added 2026-05-15 to close
# the daemon-progress visibility gap). Passed to ops.py via --run-id so
# every event — the 15 --update stages AND the bash-wrapper steps below
# — shares one row family in application_log. The progress panel
# queries by run_id, so this is what makes wrapper steps show up.
RUN_ID=$("$PY" -c "import uuid; print(uuid.uuid4())")

# Helper: emit a single application_log event with the shared RUN_ID.
# Swallows logging failures (a missing event must not crash the
# wrapper). Use as:
#   _log_event INGESTION_START wrapper_audit ["optional message"]
_log_event() {
    local event_type="$1"
    local stage_name="$2"
    local msg="${3:-}"
    DATABASE_URL="${DATABASE_URL_IPV4:-$DATABASE_URL}" \
        "$PY" scripts/_log_event.py \
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

# Heartbeat-writer for the `data_operations` daemon row in
# `platform.daemon_heartbeats` — the writer side of the
# `daemon_freshness` check (added 2026-05-25 spec 008 P0). The check
# tracks four daemons: trade_monitor (writer in tpcore/trade_monitor.py)
# + engine_service + data_operations + allocator. trade_monitor writes
# from inside its 15-min loop; data_operations is a once-a-day cron and
# its writer lives HERE — fired by the EXIT trap so it ALWAYS writes
# regardless of upstream stage success or failure. The `status` field
# captures the outcome: 'healthy' on rc=0, 'degraded' otherwise; the
# daemon_freshness check is liveness-only (any row within 26h is fresh).
#
# Same UPSERT shape as tpcore/trade_monitor.py:_write_heartbeat_once.
# Failure-isolated: a heartbeat-write failure is logged but does NOT
# alter the parent script's exit code or fire a notification (the parent
# is already exiting; a heartbeat-write failure must never crash or
# spam-alert the operator).
_write_data_operations_heartbeat() {
    local rc="$1"
    # Resolve DB URL with Railway-compatible fallback. Local Mac uses
    # DATABASE_URL_IPV4 (operator's .env convention to avoid IPv6 / pgbouncer
    # quirks on the LAN). Railway only injects DATABASE_URL — so without
    # this fallback the heartbeat would silently skip in prod even though
    # the cron ran successfully (detected 2026-05-26: data_operations
    # heartbeat went 37+ hours stale on Railway while INGESTION_COMPLETE /
    # INGESTION_FAILED events fired every cycle, proving the cron itself
    # was running but the heartbeat-writer was no-op'ing).
    # Resolution order matches the script's top-of-file normalization at
    # line 50: Railway sets DATABASE_URL (IPv6 endpoint) directly; Mac
    # uses DATABASE_URL_IPV4 from .env. Prefer the v6 variant when both
    # are set (Railway-recommended path; operator directive 2026-05-26).
    local db_url="${DATABASE_URL_IPV6:-${DATABASE_URL:-${DATABASE_URL_IPV4:-}}}"
    if [[ -z "$db_url" ]]; then
        return 0
    fi
    # Reuse the script-level $PY resolved at startup. (The duplicate
    # discovery block here was rendered defective by the 2026-05-28
    # bulk-sed that hoisted $PY; collapsed to the shared var so the
    # heartbeat tracks any future interpreter-path change at one site.)
    local py="${PY:-}"
    if [[ -z "$py" || ! -x "$py" ]]; then
        echo "⚠ heartbeat: no python resolved at script start ($PY)"
        return 0
    fi
    local status="healthy"
    if [[ "$rc" -ne 0 ]]; then status="degraded"; fi
    # cwd inside the EXIT trap is whatever the parent shell's cwd is —
    # the script does `cd "$(dirname "$0")/.."` at line ~39, so `.venv`
    # resolves from repo root. Traps run in the parent process (not a
    # subshell), so this cwd is preserved through to exit.
    # Use if/then/else (not `&&/||`) for unambiguous success-vs-fail
    # logging — the `cmd && A || B` idiom would fire B on ANY non-zero
    # from A (e.g., a stdout-write error), so the logs would lie about
    # whether the heartbeat actually wrote.
    if DATABASE_URL="$db_url" "$py" -c "
import asyncio, asyncpg, os, sys
async def main():
    # statement_cache_size/jit: keep in sync with tpcore.db.build_asyncpg_pool (Supabase pooler safety)
    conn = await asyncpg.connect(os.environ['DATABASE_URL'], statement_cache_size=0, server_settings={'jit': 'off'})
    await conn.execute(
        '''
        INSERT INTO platform.daemon_heartbeats (daemon_name, last_heartbeat, status)
        VALUES ('data_operations', now(), \$1)
        ON CONFLICT (daemon_name) DO UPDATE
            SET last_heartbeat = EXCLUDED.last_heartbeat,
                status = EXCLUDED.status
        ''',
        sys.argv[1],
    )
    await conn.close()
asyncio.run(main())
" "$status" 2>&1; then
        echo "✓ data_operations heartbeat written (status=$status)"
    else
        echo "⚠ data_operations heartbeat write FAILED (status=$status) — daemon_freshness will surface this"
    fi
}

# Catch any unexpected non-zero exit too (set -e isn't on; rely on
# explicit `exit` calls but trap as safety net). Always write the
# data_operations daemon heartbeat (success or failure path) so
# `daemon_freshness` reflects the run cadence honestly.
trap '_rc=$?; rm -rf "$LOCK_DIR" 2>/dev/null; _write_data_operations_heartbeat "$_rc" || true; if [[ $_rc -ne 0 ]]; then _notify_failure "trap (unexpected)" $_rc; fi' EXIT

echo "════════════════════════════════════════════════════════════════════════"
echo "  DATA OPERATIONS WORKFLOW — $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "════════════════════════════════════════════════════════════════════════"

# Step 1+2 — download + upload via the 7-stage --update pipeline.
echo ""
echo "▶ STEP 1+2 / 6  download + upload  (ops.py --update)"
echo "────────────────────────────────────────────────────────────────────────"
# Profile-driven feed dispatch (#165): ask the EXISTING data-ops flow
# which feeds are due per their FeedProfile cadence/trigger, and run
# only those stages — instead of the blanket "every stage every run".
# Safety: if the dispatcher errors, fall back to the full sweep (never
# "nothing runs"). An empty due list is a VALID no-op (nothing's
# cadence is up) — ops.py still runs data_validation/forensics +
# Step-4 self-heal regardless, so the 100%-green gate is unaffected.
ONLY_FLAG=""
if DUE_STAGES=$(DATABASE_URL="$DATABASE_URL_IPV4" "$PY" -m tpcore.feeds 2>/tmp/feeds_dispatch.err); then
    DUE_CSV=$(echo "$DUE_STAGES" | paste -sd, - | tr -d '[:space:]')
    # Empty due list is VALID (nothing's cadence is up). Pass a
    # non-matching sentinel so ops runs only infra + Step-4 self-heal,
    # NOT the full sweep (which truthiness would wrongly do).
    ONLY_FLAG="--only ${DUE_CSV:-NONE_DUE}"
    echo "  feed dispatcher: due = [${DUE_CSV:-<none — infra/self-heal only>}]"
else
    echo "  ⚠ feed dispatcher failed (see /tmp/feeds_dispatch.err) — full sweep fallback"
fi
DATABASE_URL="$DATABASE_URL_IPV4" "$PY" scripts/ops.py \
    --update --source data_operations_daemon --run-id "$RUN_ID" $FORCE_FLAG $ONLY_FLAG
UPDATE_RC=$?
if [[ $UPDATE_RC -ne 0 ]]; then
    echo "✗ --update exited with code $UPDATE_RC — investigate before proceeding."
    echo "  Common causes: timeout (rate-limited), partial stage failure, market open."
    echo "  Self-heal already retried any transient failure once. If still red, look at:"
    echo "    SELECT * FROM platform.application_log WHERE engine='ops' AND severity='ERROR' ORDER BY recorded_at DESC LIMIT 10;"
    _notify_failure "ops.py --update" $UPDATE_RC
    exit $UPDATE_RC
fi

# Step 3 — cross-table referential audit + auto-remediation
# (#186(5)). tpcore.auditheal runs the structured cross-table audit,
# auto-runs the proven cross_ref_cleanup remediation for the
# tradier_options_chains expired/orphan class, re-audits, and exits
# 1 on any unremediated/escalate-only red (now an ENFORCED gate —
# previously audit_all_tables always exited 0).
echo ""
echo "▶ STEP 3 / 6  cross-table audit + auto-remediation"
echo "────────────────────────────────────────────────────────────────────────"
_log_event INGESTION_START wrapper_audit
DATABASE_URL="$DATABASE_URL_IPV4" "$PY" -m tpcore.auditheal
AUDIT_RC=$?
if [[ $AUDIT_RC -ne 0 ]]; then
    _log_event INGESTION_FAILED wrapper_audit "audit exited $AUDIT_RC"
    echo "✗ auditheal exited $AUDIT_RC"
    _notify_failure "auditheal" $AUDIT_RC
    exit $AUDIT_RC
fi
_log_event INGESTION_COMPLETE wrapper_audit

# Step 4 — AUTONOMOUS SELF-HEAL (rebuilt 2026-05-16: thin caller).
#
# The bespoke bash heal loop was replaced by the generic tpcore
# self-heal engine (architecture mandate, TODO #132). All logic —
# run data_validation, read reds, dispatch each red to its HealSpec,
# run the bounded canonical repair, re-validate, bounded retry,
# honest escalation — lives in `python -m tpcore.selfheal` and is
# unit-tested. This wrapper only enforces the process contract:
#
#   exit 0  → data layer is 100% green (after 0+ autonomous repairs)
#   exit !0 → escalation; NOT green
#
# Safety invariant unchanged: DATA_OPERATIONS_COMPLETE (Step 6) is
# NEVER emitted unless this returns 0, so the engine-service daemon
# can never fire the engine sweep on unhealed data. Per-source heal
# capability is added by registering a HealSpec — never by editing
# this script (one canonical mechanism, no bash spider-web).
echo ""
echo "▶ STEP 4 / 6  autonomous self-heal (tpcore.selfheal) — guarantee 100% green"
echo "─────────────────────────────────────────────────────────────────"
_log_event INGESTION_START wrapper_selfheal
DATABASE_URL="$DATABASE_URL_IPV4" "$PY" -m tpcore.selfheal
SELFHEAL_RC=$?
if [[ $SELFHEAL_RC -ne 0 ]]; then
    _log_event INGESTION_FAILED wrapper_selfheal "self-heal escalated (rc $SELFHEAL_RC)"
    echo "✗ self-heal could not reach 100% green — escalated (see output above)."
    echo "  NOT emitting DATA_OPERATIONS_COMPLETE; engines will NOT trade on"
    echo "  unhealed data. Operator investigation required."
    _notify_failure "self-heal escalated" $SELFHEAL_RC
    exit $SELFHEAL_RC
fi
_log_event INGESTION_COMPLETE wrapper_selfheal
echo "✓ data layer 100% green (autonomous self-heal)"

# Step 4b — refresh dashboard matview now that prices_daily is current.
# REFRESH CONCURRENTLY so dashboard reads don't block while it runs (~1s).
echo ""
echo "▶ STEP 4b / 6  refresh platform.prices_daily_tickers matview"
echo "────────────────────────────────────────────────────────────────────────"
_log_event INGESTION_START wrapper_matview_refresh
if DATABASE_URL="$DATABASE_URL_IPV4" "$PY" -c "
import asyncio, asyncpg, os
async def main():
    # statement_cache_size/jit: keep in sync with tpcore.db.build_asyncpg_pool (Supabase pooler safety)
    conn = await asyncpg.connect(os.environ['DATABASE_URL'], statement_cache_size=0, server_settings={'jit': 'off'})
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
# Closes the "audit is theatre" gap: audit_data_pipeline.py used to be
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
DATABASE_URL="$DATABASE_URL_IPV4" "$PY" scripts/audit_data_pipeline.py
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

# Step 4d — DATA SUPERVISOR (per-source hold + autonomous auto-clear).
# Runs AFTER self-heal (Step 4) / deep-audit (Step 4c) so it sees the
# cycle's FINAL red set. STATE-TRACKING ONLY: it NEVER gates — exit is
# always 0 and it does NOT affect whether DATA_OPERATIONS_COMPLETE is
# emitted (that remains exclusively the Step-4/4c 100%-green decision,
# unchanged). Opens a per-source DATA_SOURCE_HELD for still-red
# sources, autonomously auto-clears recovered ones, escalates a
# chronically-stuck source. Data-native symmetric counterpart of the
# engine DA-1 supervisor.
echo ""
echo "▶ STEP 4d / 6  data supervisor (per-source hold + auto-clear)"
echo "────────────────────────────────────────────────────────────────────────"
_log_event INGESTION_START wrapper_datasupervisor
DATABASE_URL="${DATABASE_URL_IPV4:-$DATABASE_URL}" "$PY" -m tpcore.datasupervisor || true
_log_event INGESTION_COMPLETE wrapper_datasupervisor

# Step 4e — allocator heartbeat (writes platform.daemon_heartbeats so
# daemon_freshness check stops surfacing 'allocator' as STALE; spawns
# the allocator subprocess if should_fire() returns True). The
# heartbeat function is a thin safety net — should_fire's gate ladder
# (profiled → cadence → market-closed → supervisor hold → data ready →
# not already ran) handles the no-op cases structurally, so calling it
# every daily-ops cycle is idempotent. Added 2026-05-29: there's no
# Railway service for the allocator (engine_dispatch.py event-driven
# path covers the canonical fire); without this call the heartbeat
# row would never write, daemon_freshness stays RED, and
# DATA_OPERATIONS_COMPLETE never emits.
echo ""
echo "▶ STEP 4e / 6  allocator heartbeat"
echo "────────────────────────────────────────────────────────────────────────"
_log_event INGESTION_START wrapper_allocator_heartbeat
DATABASE_URL="${DATABASE_URL_IPV4:-$DATABASE_URL}" "$PY" -m ops.allocator_heartbeat || true
_log_event INGESTION_COMPLETE wrapper_allocator_heartbeat

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
    DATABASE_URL="$DATABASE_URL_IPV4" "$PY" -c "
import asyncio, asyncpg, os, uuid
async def main():
    # statement_cache_size/jit: keep in sync with tpcore.db.build_asyncpg_pool (Supabase pooler safety)
    conn = await asyncpg.connect(os.environ['DATABASE_URL'], statement_cache_size=0, server_settings={'jit': 'off'})
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
