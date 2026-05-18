#!/usr/bin/env bash
# One-button install of every launchd LaunchAgent the platform needs.
# Idempotent: existing agents are unloaded + reloaded.
#
# After this runs, the operator's mac will:
#   * keep ops.engine_service running 24/7 — the single consolidated
#     engine daemon: DATA_OPERATIONS_COMPLETE-triggered sweep + co-hosted
#     trade-monitor stream + day-rollover weekly-digest trigger
#   * keep ops.data_repair_service running 24/7 (data-lane) — polls
#     application_log for ENGINE_DATA_REQUEST, runs the canonical
#     self-heal, and emits exactly one terminal reply per request_id
#   * keep ops.llm_triage_service running 24/7 (advisory-lane) — polls
#     application_log for DATA_REPAIR_ESCALATED / DATA_SOURCE_ESCALATED
#     and fires one advisory triage pass (may open a draft, human-
#     merge-only PR; never repairs/trades/merges)
#   * run scripts/run_data_operations.sh every weekday at 21:30 UTC
#     (data-lane cron; chains: data refresh → audit → validate →
#     compress → emit event)
#
# Note: the allocator is no longer a launchd daemon (retired 2026-05-17,
# Sub-project C). It now runs as the first gated step in
# ops/engine_dispatch.py (event-driven, WEEKLY_FIRST_TRADING_DAY).
#
# Note: trade_monitor + the weekly-digest cron-trigger are no longer
# their own launchd daemons (retired 2026-05-18, DA-3). Both are folded
# into the single engine daemon (ops/engine_service.py). The daemon set
# is now: engine-service (consolidated sweep + trade-monitor +
# weekly-digest trigger), data-repair-service (data-lane),
# data-operations (data-lane cron).
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
for stale in com.michael.trading.trade-monitor com.michael.trading.weekly-digest; do
    p="$HOME/Library/LaunchAgents/${stale}.plist"
    launchctl unload "$p" 2>/dev/null || true
    rm -f "$p"
done

for installer in install_launchd_engine_service install_launchd_data_repair_service install_launchd_llm_triage_service install_launchd_data_operations; do
    echo ""
    echo "▶ ${installer}"
    echo "────────────────────────────────────────────────────────────────────────"
    scripts/${installer}.sh
done

echo ""
echo "════════════════════════════════════════════════════════════════════════"
echo "  ALL DAEMONS INSTALLED"
echo "════════════════════════════════════════════════════════════════════════"
echo ""
echo "Verify:"
echo "  launchctl list | grep com.michael.trading."
echo ""
echo "Tail logs:"
echo "  tail -f ~/Library/Logs/short-term-trading-engine/{engine-service,data-repair-service,data-operations}.log"
