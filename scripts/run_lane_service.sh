#!/usr/bin/env bash
# Long-running lane-service daemon — the consolidated DATA + ADVISORY
# (data/engine/lab-emitter) lanes co-hosted on ONE asyncio process
# under ``ops.lane_service``. Two-daemon Railway budget fix (2026-05-21):
# fuses the previous data-repair-service + llm-triage-service into one
# daemon so the closed-whitelist installer loop is engine-service +
# lane-service + data-operations (2 long-lived daemons + 1 cron =
# 2-daemon Railway budget).
#
# The four co-tasks (data_repair / triage_data / triage_engine /
# triage_lab_emitter) are crash-isolated via per-lane
# ``_run_supervised`` restart-on-error wrappers — a crashed lane never
# brings down a sibling or the daemon.
#
# Usage: run in a separate terminal OR install via launchd LaunchAgent
# (scripts/install_launchd_lane_service.sh).
# Exits cleanly on SIGINT / SIGTERM; KeepAlive restarts on crash.
#
# Logs to ~/Library/Logs/short-term-trading-engine/lane-service.log
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
    .venv/bin/python -m ops.lane_service
