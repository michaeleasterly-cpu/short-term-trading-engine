#!/usr/bin/env bash
# Long-running engine-service daemon. Polls platform.application_log
# every 60s for DATA_OPERATIONS_COMPLETE events and, when one appears,
# shells out to scripts/run_all_engines.sh.
#
# Replaces the inline engine-sweep step at the end of
# scripts/run_data_operations.sh — decouples data-ops latency / failure
# modes from engine execution.
#
# Usage: run in a separate terminal OR install via launchd LaunchAgent
# (scripts/install_launchd_engine_service.sh).
# Exits cleanly on SIGINT / SIGTERM; KeepAlive restarts on crash.
#
# Logs to ~/Library/Logs/short-term-trading-engine/engine-service.log
# when invoked via launchd; stdout otherwise.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

# IPv4 pooler — Supabase direct DSN is IPv6-only and not reachable
# from launchd's network namespace on macOS.
exec env DATABASE_URL="${DATABASE_URL_IPV4:-$DATABASE_URL}" \
    .venv/bin/python -m ops.engine_service
