#!/usr/bin/env bash
# Phase 1: Alpaca corporate actions → CSV under data/corp_actions_backfill/.
set -uo pipefail
cd "$(dirname "$0")/.."
set -a
# shellcheck disable=SC1091
source .env
set +a
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/backfill_corp_actions_csv.py "$@"
