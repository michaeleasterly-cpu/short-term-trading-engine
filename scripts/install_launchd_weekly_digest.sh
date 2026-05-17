#!/usr/bin/env bash
# Install a launchd LaunchAgent that emits the weekly data-layer
# digest on a schedule. NOT KeepAlive (it is a one-shot that exits) —
# StartCalendarInterval fires it DAILY; emit_digest is idempotent per
# ISO week so it effectively pushes once a week while being resilient
# to a missed day.
#
# The digest is the operator's non-skippable state-comprehension
# floor: every provider cutover, every self-heal + what it changed,
# every gate that passed within margin of failing, and one
# adversarially-surfaced "most likely silently wrong" item. Ack with
# `python -m ops.weekly_digest ack`; two unacked weeks
# auto-de-escalates live trading.
#
# Install:    scripts/install_launchd_weekly_digest.sh
# Uninstall:  launchctl unload ~/Library/LaunchAgents/com.michael.trading.weekly-digest.plist
set -uo pipefail
cd "$(dirname "$0")/.."

REPO_ROOT="$(pwd)"
AGENT_LABEL="com.michael.trading.weekly-digest"
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
        <string>cd ${REPO_ROOT} &amp;&amp; ${REPO_ROOT}/scripts/run_weekly_digest.sh</string>
    </array>

    <!-- One-shot, not KeepAlive. RunAtLoad emits immediately on
         install; StartCalendarInterval re-fires daily (Hour 14 local
         ≈ post-data-ops). emit is idempotent per ISO week. -->
    <key>RunAtLoad</key>
    <true/>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>14</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/weekly-digest.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/weekly-digest.err</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
EOF

echo "✓ wrote $PLIST_PATH"
echo "  emits weekly digest (idempotent/ISO-week); ack via:"
echo "  python -m ops.weekly_digest ack"

launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
echo "✓ loaded ${AGENT_LABEL}"
