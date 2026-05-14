#!/usr/bin/env bash
# Install a launchd LaunchAgent that runs scripts/ops.py --allocate every
# Monday at 13:00 UTC. Per expert recommendation 2026-05-13.
#
# Why Monday 13:00 UTC:
#   * After risk_state.weekly_reset_at fires
#   * Before NYSE open (14:30 UTC EDT / 15:30 UTC EST)
#   * Operator in Manila (UTC+8) sees results Tuesday morning
#
# Paper mode (default): allocator records freeze states but doesn't
# write risk_state.kill_switch_active. Switch to --enforce-freeze when
# the first engine flips to live capital.
#
# Install:    scripts/install_launchd_allocator.sh
# Uninstall:  launchctl unload ~/Library/LaunchAgents/com.michael.trading.allocator.plist
set -uo pipefail
cd "$(dirname "$0")/.."

REPO_ROOT="$(pwd)"
AGENT_LABEL="com.michael.trading.allocator"
PLIST_PATH="$HOME/Library/LaunchAgents/${AGENT_LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/short-term-trading-engine"

mkdir -p "$LOG_DIR"

# Convert 13:00 UTC to the Mac's local time (launchd's
# StartCalendarInterval wants local). See install_launchd_data_operations.sh
# for the rationale — `date -j -u -f` was a no-op.
TODAY_UTC=$(date -u '+%Y-%m-%d')
EPOCH=$(TZ=UTC date -j -f '%Y-%m-%d %H:%M' "$TODAY_UTC 13:00" '+%s' 2>/dev/null || echo "")
if [[ -n "$EPOCH" ]]; then
    LOCAL_HH=$(date -j -r "$EPOCH" '+%H')
    LOCAL_MM=$(date -j -r "$EPOCH" '+%M')
else
    echo "⚠ TZ conversion failed; defaulting to 13:00 local" >&2
    LOCAL_HH=13
    LOCAL_MM=0
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
        <string>cd ${REPO_ROOT} &amp;&amp; set -a &amp;&amp; source .env &amp;&amp; set +a &amp;&amp; DATABASE_URL=\$DATABASE_URL_IPV4 ${REPO_ROOT}/.venv/bin/python ${REPO_ROOT}/scripts/ops.py --allocate</string>
    </array>

    <!-- Monday only (Weekday=1) -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key><integer>1</integer>
        <key>Hour</key><integer>${LOCAL_HH}</integer>
        <key>Minute</key><integer>${LOCAL_MM}</integer>
    </dict>

    <key>RunAtLoad</key>
    <false/>

    <key>KeepAlive</key>
    <false/>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/allocator.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/allocator.err</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
EOF

echo "✓ wrote $PLIST_PATH"
echo "  fires Monday ${LOCAL_HH}:${LOCAL_MM} local (= 13:00 UTC) — weekly allocator"
echo "  logs:  $LOG_DIR/allocator.log + allocator.err"

launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
echo "✓ launchd loaded"

echo ""
echo "To uninstall:  launchctl unload \"$PLIST_PATH\" && rm \"$PLIST_PATH\""
