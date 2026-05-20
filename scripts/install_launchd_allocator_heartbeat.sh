#!/usr/bin/env bash
# Install a launchd LaunchAgent for the allocator heartbeat — a THIN
# safety-net cron, NOT a primary trigger.
#
# The allocator is event-driven on DATA_OPERATIONS_COMPLETE via
# ops/engine_dispatch.py (Sub-project C, PR #17, 2026-05-17). This
# heartbeat fires once per weekday at 22:30 UTC (1h after the
# data-operations cron at 21:30 UTC — generous buffer for engine_service
# to consume the event + dispatch the allocator first). The wrapper
# (scripts/run_allocator_heartbeat.sh) consults
# tpcore.engine_profile.should_fire (same canonical gate as the
# dispatcher) and exits clean on Tue–Fri, on non-cadence-boundary
# Mondays, or when the daemon already ran the allocator this cycle.
#
# Why fire every weekday (not just Monday): if Monday is a NYSE holiday
# the first trading day of the week is Tuesday — should_fire is the
# trading-calendar-aware gate, not the OS clock. Daily firing + the
# in-process gate honors the calendar without launchd having to know
# about it. Mirrors the data-operations cron's same approach.
#
# Outside the install_all_daemons.sh closed-whitelist for-loop: the
# two-daemon invariant test (scripts/tests/test_two_daemon_invariant.py)
# pins the loop tokens to exactly the 4 long-lived/cron installers. This
# heartbeat is a sibling installer call, NOT a member of the loop.
#
# Install:    scripts/install_launchd_allocator_heartbeat.sh
# Uninstall:  launchctl unload ~/Library/LaunchAgents/com.michael.trading.allocator-heartbeat.plist
set -uo pipefail
cd "$(dirname "$0")/.."

REPO_ROOT="$(pwd)"
AGENT_LABEL="com.michael.trading.allocator-heartbeat"
PLIST_PATH="$HOME/Library/LaunchAgents/${AGENT_LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/short-term-trading-engine"

mkdir -p "$LOG_DIR"

# Convert 22:30 UTC to the Mac's local time (launchd's
# StartCalendarInterval wants local time; same conversion the
# data-operations installer does at 21:30 UTC — see comments there).
TODAY_UTC=$(date -u '+%Y-%m-%d')
EPOCH=$(TZ=UTC date -j -f '%Y-%m-%d %H:%M' "$TODAY_UTC 22:30" '+%s' 2>/dev/null || echo "")
if [[ -n "$EPOCH" ]]; then
    LOCAL_HH=$(date -j -r "$EPOCH" '+%H')
    LOCAL_MM=$(date -j -r "$EPOCH" '+%M')
else
    echo "⚠ TZ conversion failed; defaulting to 14:30 local" >&2
    LOCAL_HH=14
    LOCAL_MM=30
fi
LOCAL_HH=${LOCAL_HH#0}
LOCAL_MM=${LOCAL_MM#0}
[[ -z "$LOCAL_HH" ]] && LOCAL_HH=0
[[ -z "$LOCAL_MM" ]] && LOCAL_MM=0

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${AGENT_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-lc</string>
        <string>cd ${REPO_ROOT} &amp;&amp; ${REPO_ROOT}/scripts/run_allocator_heartbeat.sh</string>
    </array>

    <!-- Fires every day at ${LOCAL_HH}:${LOCAL_MM} local (= 22:30 UTC year-round).
         Non-trading days + already-ran-this-cycle days are harmless
         no-ops via should_fire's calendar-aware cadence gate — same
         pattern data-operations.plist uses (it fires daily and lets the
         in-process market-closed pre-flight handle weekends/holidays). -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>${LOCAL_HH}</integer>
        <key>Minute</key><integer>${LOCAL_MM}</integer>
    </dict>

    <key>RunAtLoad</key>
    <false/>

    <key>KeepAlive</key>
    <false/>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/allocator-heartbeat.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/allocator-heartbeat.err</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
EOF

echo "✓ wrote $PLIST_PATH"
echo "  fires daily at ${LOCAL_HH}:${LOCAL_MM} local time (= 22:30 UTC year-round)"
echo "  should_fire gate is the calendar-aware boundary — Tue–Fri / non-cadence Mondays no-op"
echo "  logs:   $LOG_DIR/allocator-heartbeat.log + allocator-heartbeat.err"

launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
echo "✓ launchd loaded — allocator-heartbeat is now scheduled"

launchctl list "$AGENT_LABEL" 2>&1 | grep -E "Label|LastExit|PID" || true
echo ""
echo "To uninstall:  launchctl unload \"$PLIST_PATH\" && rm \"$PLIST_PATH\""
