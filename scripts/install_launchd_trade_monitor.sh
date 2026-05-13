#!/usr/bin/env bash
# Install a launchd LaunchAgent that keeps the trade_monitor daemon
# running. Auto-restarts on crash via KeepAlive=true.
#
# trade_monitor is required for Sigma + Reversion Tier-2 cascade — when
# the bracket entry fills, the monitor submits the limit-sell at the
# further take-profit. Without the monitor running, those positions sit
# at Tier 1 indefinitely (the YUMC orphan pattern).
#
# Install:    scripts/install_launchd_trade_monitor.sh
# Uninstall:  launchctl unload ~/Library/LaunchAgents/com.michael.trading.trade-monitor.plist
set -uo pipefail
cd "$(dirname "$0")/.."

REPO_ROOT="$(pwd)"
AGENT_LABEL="com.michael.trading.trade-monitor"
PLIST_PATH="$HOME/Library/LaunchAgents/${AGENT_LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/short-term-trading-engine"

mkdir -p "$LOG_DIR"

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
        <string>cd ${REPO_ROOT} &amp;&amp; ${REPO_ROOT}/scripts/run_trade_monitor.sh</string>
    </array>

    <!-- Run at load + restart on exit. KeepAlive ensures the daemon
         comes back if it crashes or the Mac wakes from sleep. -->
    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
        <key>Crashed</key>
        <true/>
    </dict>

    <!-- Throttle the auto-restart to avoid runaway respawn -->
    <key>ThrottleInterval</key>
    <integer>30</integer>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/trade-monitor.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/trade-monitor.err</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
EOF

echo "✓ wrote $PLIST_PATH"
echo "  runs at load + auto-restarts on crash"
echo "  logs:  $LOG_DIR/trade-monitor.log + trade-monitor.err"

launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
echo "✓ launchd loaded — trade_monitor is now running and persistent"
echo ""
echo "Verify with: launchctl list | grep ${AGENT_LABEL}"
echo "Tail logs:   tail -f $LOG_DIR/trade-monitor.log"
echo ""
echo "To uninstall:  launchctl unload \"$PLIST_PATH\" && rm \"$PLIST_PATH\""
