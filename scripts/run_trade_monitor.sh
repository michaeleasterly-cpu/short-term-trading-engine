#!/usr/bin/env bash
# Long-running trade_monitor daemon. Required for Sigma + Reversion Tier 2
# cascade (limit-sell-on-Tier-1-fill); Momentum doesn't need it (no
# per-position stops between rebalances).
#
# Usage: run in a separate terminal OR install via launchd LaunchAgent.
# Reconnects automatically with exponential backoff (max 60s).
# Exits only on Ctrl-C / SIGTERM.
#
# Logs to ~/Library/Logs/short-term-trading-engine/trade-monitor.log
# when invoked via launchd; stdout otherwise.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

exec .venv/bin/python -m tpcore.trade_monitor
