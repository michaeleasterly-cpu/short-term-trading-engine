#!/usr/bin/env bash
# Phase 1: FMP fundamentals → CSV under data/fmp_backfill/.
set -uo pipefail
cd "$(dirname "$0")/.."
set -a
# shellcheck disable=SC1091
source .env
set +a
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/backfill_fmp_csv.py "$@"
