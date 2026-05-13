#!/usr/bin/env bash
# One-shot cleanup of structurally bad rows in platform.prices_daily —
# rows that violate the validation suite's row_integrity check (close<=0,
# high<low, NULLs, future dates). All such rows are from the deprecated
# Tradier ingestion source.
#
# Default: dry-run (prints what would be deleted, no DB writes).
# Pass --confirm to actually DELETE.
#
# Each deletion is recorded in platform.application_log as a
# DATA_CLEANUP event with the full row payload, so the audit trail is
# queryable forever.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/cleanup_bad_price_rows.py "$@"
