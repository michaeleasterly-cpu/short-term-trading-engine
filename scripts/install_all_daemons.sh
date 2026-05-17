#!/usr/bin/env bash
# One-button install of every launchd LaunchAgent the platform needs.
# Idempotent: existing agents are unloaded + reloaded.
#
# After this runs, the operator's mac will:
#   * keep tpcore.trade_monitor running 24/7 (auto-restart on crash)
#   * keep ops.engine_service running 24/7 — polls application_log for
#     DATA_OPERATIONS_COMPLETE and fires the engine sweep when seen
#   * keep ops.data_repair_service running 24/7 — polls application_log
#     for ENGINE_DATA_REQUEST, runs the canonical self-heal, and emits
#     exactly one terminal reply per request_id
#   * run scripts/run_data_operations.sh every weekday at 21:30 UTC
#     (chains: data refresh → audit → validate → compress → emit event)
#   * run scripts/ops.py --allocate every Monday at 13:00 UTC
#   * emit the weekly data-layer digest (idempotent/ISO-week) — the
#     operator's state-comprehension floor; unacked 2 weeks auto-
#     de-escalates live trading
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

for installer in install_launchd_trade_monitor install_launchd_engine_service install_launchd_data_repair_service install_launchd_data_operations install_launchd_allocator install_launchd_weekly_digest; do
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
echo "  tail -f ~/Library/Logs/short-term-trading-engine/{trade-monitor,engine-service,data-repair-service,data-operations,allocator,weekly-digest}.log"
