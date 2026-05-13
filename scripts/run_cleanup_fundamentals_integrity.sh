#!/usr/bin/env bash
# Clean up fundamentals_quarterly integrity violations:
#   * period_end_date > filing_date (DELETE the row)
#   * shares_outstanding <= 0       (UPDATE to NULL)
# Default: dry-run. Pass --confirm to apply.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/cleanup_fundamentals_integrity.py "$@"
