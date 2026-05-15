#!/usr/bin/env bash
# One-shot wrapper: backfill SH, PSQ, GLD daily bars into prices_daily.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
# shellcheck disable=SC2046
export $(grep -E '^DATABASE_URL_IPV4=|^ALPACA_' .env | xargs)
export DATABASE_URL="${DATABASE_URL_IPV4:?DATABASE_URL_IPV4 missing}"
"$REPO_ROOT/.venv/bin/python" scripts/backfill_sentinel_etfs.py
