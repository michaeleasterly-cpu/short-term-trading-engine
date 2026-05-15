#!/usr/bin/env bash
# Sentinel backtest — historical macro defense.
# Wrapped per the "wrap multi-flag commands in scripts/" rule.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# shellcheck disable=SC2046
export $(grep -E '^DATABASE_URL_IPV4=' .env | xargs)
export DATABASE_URL="${DATABASE_URL_IPV4:?DATABASE_URL_IPV4 missing from .env}"

"$REPO_ROOT/.venv/bin/python" sentinel/backtest.py \
    --start 2018-01-01 \
    --end 2025-12-31 \
    "$@"
