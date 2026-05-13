#!/usr/bin/env bash
# Run the weekly Allocator rebalance.
# Default: paper mode (no kill_switch enforcement). Pass --enforce-freeze
# to write risk_state.kill_switch_active on hard freeze (live trading).
set -uo pipefail
cd "$(dirname "$0")/.."
set -a
# shellcheck disable=SC1091
source .env
set +a
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/run_allocator.py "$@"
