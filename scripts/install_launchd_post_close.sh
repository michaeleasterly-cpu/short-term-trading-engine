#!/usr/bin/env bash
# Install a launchd LaunchAgent that runs run_post_close.sh nightly at
# 21:30 UTC weekdays (Mon-Fri). Per expert recommendation 2026-05-13.
#
# Why 21:30 UTC year-round (one fire, not two):
#   - Summer (EDT): 17:30 ET = 90 min after 16:00 close → bars published
#   - Winter (EST): 16:30 ET = 30 min after 16:00 close → bars published
#   Operator in Manila (UTC+8) gets results at 05:30 local = wakes to a
#   green/red signal at 06:00-08:00 local. Failures wait; success is silent.
#
# Weekdays only — Saturday/Sunday have no new bars; running just re-
# validates stale-and-OK data and burns FMP quota. NYSE holidays will
# fire and likely no-op cleanly (validation tolerates "no new bars").
#
# Retry policy: launchd's KeepAlive=true retries on crash, which would
# retry-spam a real failure. Instead, retries live INSIDE run_post_close.sh
# via ops.py's self-heal pass — bounded to one transient retry per stage.
#
# Logs: ~/Library/Logs/short-term-trading-engine/post-close-YYYY-MM-DD.log
#
# Install:    scripts/install_launchd_post_close.sh
# Uninstall:  launchctl unload ~/Library/LaunchAgents/com.michael.trading.post-close.plist
set -uo pipefail
cd "$(dirname "$0")/.."

REPO_ROOT="$(pwd)"
AGENT_LABEL="com.michael.trading.post-close"
PLIST_PATH="$HOME/Library/LaunchAgents/${AGENT_LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/short-term-trading-engine"

mkdir -p "$LOG_DIR"

# Convert 21:30 UTC to the Mac's local time (launchd's StartCalendarInterval
# wants local time). Earlier `date -j -u -f` approach was a no-op because
# `-u` makes both input AND output UTC; the correct path is: compute the
# epoch for today at 21:30 UTC, then format that epoch without `-u`.
TODAY_UTC=$(date -u '+%Y-%m-%d')
EPOCH=$(TZ=UTC date -j -f '%Y-%m-%d %H:%M' "$TODAY_UTC 21:30" '+%s' 2>/dev/null || echo "")
if [[ -n "$EPOCH" ]]; then
    LOCAL_HH=$(date -j -r "$EPOCH" '+%H')
    LOCAL_MM=$(date -j -r "$EPOCH" '+%M')
else
    # Fallback only if the conversion fails — log it loudly.
    echo "⚠ TZ conversion failed; defaulting to 13:30 local" >&2
    LOCAL_HH=13
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
        <string>cd ${REPO_ROOT} &amp;&amp; ${REPO_ROOT}/scripts/run_post_close.sh</string>
    </array>

    <!-- Fires every day at ${LOCAL_HH}:${LOCAL_MM} local (= 21:30 UTC).
         Weekend / holiday runs are harmless no-ops via the
         market-closed pre-flight + daily_bars idempotency, which is
         simpler than weekday-filtering across timezones (launchd
         Weekday is LOCAL day, but 21:30 UTC crosses midnight in
         Manila so a fixed Weekday=1-5 list misaligns by one). -->
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
    <string>${LOG_DIR}/post-close.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/post-close.err</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
EOF

echo "✓ wrote $PLIST_PATH"
echo "  fires Mon-Fri at ${LOCAL_HH}:${LOCAL_MM} local time (= 21:30 UTC year-round)"
echo "  logs:   $LOG_DIR/post-close.log + post-close.err"

# Load (or reload) the agent.
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
echo "✓ launchd loaded"

# Show next scheduled run.
launchctl list "$AGENT_LABEL" 2>&1 | grep -E "Label|LastExit|PID" || true
echo ""
echo "To uninstall:  launchctl unload \"$PLIST_PATH\" && rm \"$PLIST_PATH\""
