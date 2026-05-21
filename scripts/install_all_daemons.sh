#!/usr/bin/env bash
# One-button install of every launchd LaunchAgent the platform needs.
# Idempotent: existing agents are unloaded + reloaded.
#
# After this runs, the operator's mac will:
#   * keep ops.engine_service running 24/7 — the single consolidated
#     engine daemon: DATA_OPERATIONS_COMPLETE-triggered sweep + co-hosted
#     trade-monitor stream + day-rollover weekly-digest trigger
#   * keep ops.lane_service running 24/7 — the consolidated
#     data-lane + advisory-lane daemon (2026-05-21 Railway 2-daemon
#     budget fix: fuses the previous data-repair-service and
#     llm-triage-service into ONE asyncio process under
#     ``asyncio.gather()``). Four crash-isolated co-tasks on ONE pool:
#       - data_repair: polls application_log for ENGINE_DATA_REQUEST,
#         runs the canonical self-heal, emits exactly one terminal
#         reply per request_id (the previous data-repair-service).
#       - triage_data: AUTONOMOUS data-recovery on
#         DATA_REPAIR_ESCALATED / DATA_SOURCE_ESCALATED /
#         INGESTION_AUTO_RECOVERY_FAILED via the frozen whitelist in
#         ops/llm_data_recovery.py (single-shot per cycle; no draft
#         PR, no human-merge gate).
#       - triage_engine: still PR-GATED engine triage on
#         ENGINE_ESCALATED via ops.engine_llm_triage.
#       - triage_lab_emitter: SP-G operator-command path
#         (/lab-spec-emit); event-trigger set is empty per operator Q6.
#   * run scripts/run_data_operations.sh every weekday at 21:30 UTC
#     (data-lane cron; chains: data refresh → audit → validate →
#     compress → emit event)
#
# Note: the allocator is no longer a primary-trigger launchd daemon
# (retired 2026-05-17, Sub-project C). Its primary trigger is now the
# first gated step in ops/engine_dispatch.py (event-driven on
# DATA_OPERATIONS_COMPLETE, WEEKLY_FIRST_TRADING_DAY). A thin SAFETY-NET
# cron (com.michael.trading.allocator-heartbeat) fires daily at 22:30
# UTC and exits clean unless tpcore.engine_profile.should_fire returns
# fire=True (i.e. engine_service was down and the cycle's allocation
# never landed). Installed OUTSIDE the closed-whitelist for-loop below.
#
# Note: trade_monitor + the weekly-digest cron-trigger are no longer
# their own launchd daemons (retired 2026-05-18, DA-3). Both are folded
# into the single engine daemon (ops/engine_service.py).
#
# Note: data-repair-service + llm-triage-service are no longer their
# own launchd daemons (retired 2026-05-21, Railway 2-daemon budget).
# Both are folded into the single lane daemon (ops/lane_service.py)
# as four crash-isolated asyncio.gather() co-tasks. The daemon set is
# now: engine-service (consolidated sweep + trade-monitor +
# weekly-digest trigger), lane-service (data-repair + 3 triage lanes),
# data-operations (data-lane cron). Two long-lived daemons + one cron
# = fits Railway's 2-daemon limit.
#
# Logs go to ~/Library/Logs/short-term-trading-engine/.
#
# Uninstall everything:
#   launchctl unload ~/Library/LaunchAgents/com.michael.trading.*.plist
#   rm ~/Library/LaunchAgents/com.michael.trading.*.plist
set -uo pipefail
cd "$(dirname "$0")/.."

echo "════════════════════════════════════════════════════════════════════════"
echo "  INSTALLING PLATFORM DAEMONS — $(date '+%Y-%m-%d %H:%M:%S')"
echo "════════════════════════════════════════════════════════════════════════"

# allocator retired from launchd 2026-05-17 (Sub-project C): now the
# first gated step in ops/engine_dispatch.py (event-driven, WEEKLY).

# DA-3 (2026-05-18): trade_monitor + weekly_digest folded into the
# single engine daemon (ops/engine_service.py). Retire their launchd
# plists idempotently — a deleted per-installer cannot self-unload,
# and a still-loaded trade-monitor plist would run a SECOND Tier-2
# cascade (H-3). Symmetric to Sub-project C retiring the allocator cron.
#
# 2026-05-21 (Railway 2-daemon budget): data-repair-service +
# llm-triage-service folded into the single lane daemon
# (ops/lane_service.py). Symmetric idempotent retirement so a still-
# loaded plist of the old daemons would not poll the bus twice
# (terminal-event duplicate-emit risk) alongside the new lane-service.
for stale in com.michael.trading.trade-monitor com.michael.trading.weekly-digest com.michael.trading.data-repair-service com.michael.trading.llm-triage-service; do
    p="$HOME/Library/LaunchAgents/${stale}.plist"
    launchctl unload "$p" 2>/dev/null || true
    rm -f "$p"
done

for installer in install_launchd_engine_service install_launchd_lane_service install_launchd_data_operations; do
    echo ""
    echo "▶ ${installer}"
    echo "────────────────────────────────────────────────────────────────────────"
    scripts/${installer}.sh
done

# ── Sibling installers (OUTSIDE the closed-whitelist loop) ──────────────
# scripts/tests/test_two_daemon_invariant.py pins the loop tokens above
# to exactly the 3 long-lived/cron installers (engine-service +
# lane-service + data-operations) — fits the Railway 2-daemon budget
# (engine + lane = 2 long-lived daemons; data-operations is a cron,
# not a daemon). Anything below this line is a thin sibling cron /
# safety-net, NOT a member of the closed whitelist; the test
# deliberately ignores out-of-loop calls.
echo ""
echo "▶ install_launchd_allocator_heartbeat (sibling cron — safety-net only)"
echo "────────────────────────────────────────────────────────────────────────"
scripts/install_launchd_allocator_heartbeat.sh

echo ""
echo "════════════════════════════════════════════════════════════════════════"
echo "  ALL DAEMONS INSTALLED"
echo "════════════════════════════════════════════════════════════════════════"
echo ""
echo "Verify:"
echo "  launchctl list | grep com.michael.trading."
echo ""
echo "Tail logs:"
echo "  tail -f ~/Library/Logs/short-term-trading-engine/{engine-service,lane-service,data-operations}.log"
