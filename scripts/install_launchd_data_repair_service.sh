#!/usr/bin/env bash
# Install a launchd LaunchAgent that keeps the data-repair-service
# daemon running. Auto-restarts on crash via KeepAlive=true.
#
# data-repair-service polls platform.application_log for
# ENGINE_DATA_REQUEST events and, for each, runs the canonical bounded
# self-heal then emits exactly one terminal reply
# (DATA_REPAIR_COMPLETE / DATA_REPAIR_ESCALATED) keyed by request_id.
# Without it running, an engine that requests a data repair blocks
# forever waiting for a reply that never comes.
#
# Install:    scripts/install_launchd_data_repair_service.sh
# Uninstall:  launchctl unload ~/Library/LaunchAgents/com.michael.trading.data-repair-service.plist
set -uo pipefail
cd "$(dirname "$0")/.."

REPO_ROOT="$(pwd)"
AGENT_LABEL="com.michael.trading.data-repair-service"
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
        <string>cd ${REPO_ROOT} &amp;&amp; ${REPO_ROOT}/scripts/run_data_repair_service.sh</string>
    </array>

    <!-- Run at load + always restart on exit. KeepAlive=<true/> means
         launchd respawns regardless of exit reason. The narrower
         dict-form (Crashed=true) only catches signal-based crashes —
         Python tracebacks (exit code 1) are neither "successful" nor
         "crashed" and leave the process as a non-restarting zombie. -->
    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <!-- Throttle the auto-restart to avoid runaway respawn -->
    <key>ThrottleInterval</key>
    <integer>30</integer>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/data-repair-service.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/data-repair-service.err</string>

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
echo "  logs:  $LOG_DIR/data-repair-service.log + data-repair-service.err"

launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
echo "✓ launchd loaded — data-repair-service is now running and persistent"
echo ""
echo "Verify with: launchctl list | grep ${AGENT_LABEL}"
echo "Tail logs:   tail -f $LOG_DIR/data-repair-service.log"
echo ""
echo "To uninstall:  launchctl unload \"$PLIST_PATH\" && rm \"$PLIST_PATH\""
