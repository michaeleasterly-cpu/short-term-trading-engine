#!/usr/bin/env bash
# Phase 2: load FMP CSV → fundamentals_quarterly.
set -uo pipefail
cd "$(dirname "$0")/.."
set -a
# shellcheck disable=SC1091
source .env
set +a
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/load_fmp_csv.py "$@"
