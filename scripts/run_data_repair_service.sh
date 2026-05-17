#!/usr/bin/env bash
# Long-running data-repair-service daemon. Polls platform.application_log
# every 60s for ENGINE_DATA_REQUEST events and, for each one, runs the
# canonical bounded self-heal (tpcore.selfheal) then emits exactly one
# terminal reply (DATA_REPAIR_COMPLETE or DATA_REPAIR_ESCALATED) keyed
# by the request's request_id.
#
# This is the DATA side of the engine/data request-response handshake;
# ops/engine_service.py is the engine side. It serializes against
# scripts/run_data_operations.sh Step-4 self-heal via the shared
# ${TMPDIR:-/tmp}/ste-data-operations.lock directory.
#
# Usage: run in a separate terminal OR install via launchd LaunchAgent
# (scripts/install_launchd_data_repair_service.sh).
# Exits cleanly on SIGINT / SIGTERM; KeepAlive restarts on crash.
#
# Logs to ~/Library/Logs/short-term-trading-engine/data-repair-service.log
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
    .venv/bin/python -m ops.data_repair_service
