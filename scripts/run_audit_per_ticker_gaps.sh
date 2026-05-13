#!/usr/bin/env bash
# Per-ticker gap audit. Counts NYSE sessions in each ticker's lifetime
# vs actual bars present in prices_daily. Reports per-ticker coverage %.
#
# Usage:
#   scripts/run_audit_per_ticker_gaps.sh                      # all tickers
#   scripts/run_audit_per_ticker_gaps.sh --tier-le 2          # tier ≤ 2 only
#   scripts/run_audit_per_ticker_gaps.sh --tier-le 2 --emit-missing-csv
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/audit_per_ticker_gaps.py "$@"
