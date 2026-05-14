#!/usr/bin/env bash
# One-shot launchd agent that fires momentum --force-rebalance on
# 2026-05-27 22:30 UTC (= 2026-05-28 06:30 Manila local — Mac TZ is UTC+8).
#
# Built for the operator's June-1 travel: laptop will be on a plane on
# the natural first-trading-day-of-month rebalance date, so the
# rebalance is shifted forward to 2026-05-27.
#
# Timing rationale: data-operations daemon fires at 21:30 UTC and takes
# ~15-30 min; engine-service runs the regular engine sweep within
# another minute; 22:30 UTC leaves a clean 30+ min margin so the
# force-rebalance reads settled May-27 bars and doesn't race the
# regular sweep.
#
# After firing, this agent will lie dormant. Unload + remove:
#   launchctl unload ~/Library/LaunchAgents/com.michael.trading.momentum-oneshot-2026-05-27.plist
#   rm ~/Library/LaunchAgents/com.michael.trading.momentum-oneshot-2026-05-27.plist
#
# Preview the planned trades first (optional, before the scheduled fire):
#   set -a; source .env; set +a
#   DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -m momentum.scheduler --force-rebalance --dry-run
set -uo pipefail
cd "$(dirname "$0")/.."

REPO_ROOT="$(pwd)"
AGENT_LABEL="com.michael.trading.momentum-oneshot-2026-05-27"
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
        <string>cd ${REPO_ROOT} &amp;&amp; ${REPO_ROOT}/scripts/run_momentum_force_rebalance.sh</string>
    </array>

    <!-- Local time. Mac is UTC+8 (Manila), so 06:30 May 28 local =
         22:30 May 27 UTC. StartCalendarInterval fires the next time
         the wall clock matches; with Month+Day+Hour+Minute set,
         that's exactly one occurrence per year. After firing,
         unload+remove this agent to prevent re-firing in 2027. -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Month</key><integer>5</integer>
        <key>Day</key><integer>28</integer>
        <key>Hour</key><integer>6</integer>
        <key>Minute</key><integer>30</integer>
    </dict>

    <key>RunAtLoad</key>
    <false/>

    <!-- One-shot: do NOT restart. Whether it succeeds or fails, exit
         cleanly. The operator inspects logs after returning. -->
    <key>KeepAlive</key>
    <false/>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/momentum-oneshot-2026-05-27.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/momentum-oneshot-2026-05-27.err</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
EOF

echo "✓ wrote $PLIST_PATH"
echo "  fires ONCE at 2026-05-28 06:30 Manila local (= 2026-05-27 22:30 UTC)"
echo "  logs:  $LOG_DIR/momentum-oneshot-2026-05-27.{log,err}"

launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
echo "✓ launchd loaded"
echo ""
echo "Verify scheduled:"
echo "  launchctl print gui/\$UID/${AGENT_LABEL} | grep -iE 'next|state'"
echo ""
echo "After it fires, unload + remove:"
echo "  launchctl unload \"$PLIST_PATH\" && rm \"$PLIST_PATH\""
