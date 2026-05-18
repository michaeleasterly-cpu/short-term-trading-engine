#!/usr/bin/env bash
# Long-running llm-triage-service daemon. Polls platform.application_log
# every 60s for DATA_REPAIR_ESCALATED / DATA_SOURCE_ESCALATED events
# and, when one appears, fires one advisory ops.llm_data_triage.run_triage
# pass (which may open a draft, human-merge-only PR).
#
# This is the ADVISORY lane: event-driven sibling of
# ops/engine_service.py / ops/data_repair_service.py. It NEVER repairs
# data, runs a stage, mutates a table, trades, or merges — restoration
# only ever happens via the deterministic path.
#
# Usage: run in a separate terminal OR install via launchd LaunchAgent
# (scripts/install_launchd_llm_triage_service.sh).
# Exits cleanly on SIGINT / SIGTERM; KeepAlive restarts on crash.
#
# Logs to ~/Library/Logs/short-term-trading-engine/llm-triage-service.log
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
    .venv/bin/python -m ops.llm_triage_service
