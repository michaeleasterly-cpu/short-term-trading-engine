#!/usr/bin/env bash
# Phase 2: load the newest backfill CSV (or one specified by path) into
# platform.prices_daily. Re-validates every row with the same integrity
# predicate Phase 1 used. Upsert is idempotent.
#
# Usage:
#   scripts/run_load_alpaca_csv.sh                       # newest CSV
#   scripts/run_load_alpaca_csv.sh path/to/file.csv      # specific
#   scripts/run_load_alpaca_csv.sh --dry-run             # parse only
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/load_alpaca_csv.py "$@"
