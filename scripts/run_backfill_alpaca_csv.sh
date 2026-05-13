#!/usr/bin/env bash
# Phase 1: pull historical bars from Alpaca and write to CSV under
# data/alpaca_backfill/. Default: every ticker in liquidity_tiers tier <= 2,
# from 2012-01-01 to yesterday.
#
# CSV-first design: Phase 1 (this script) writes the raw Alpaca data to
# CSV; Phase 2 (run_load_alpaca_csv.sh) validates each row and upserts
# into prices_daily. If you only want to inspect what Alpaca returns,
# just run Phase 1.
#
# Usage:
#   scripts/run_backfill_alpaca_csv.sh
#   scripts/run_backfill_alpaca_csv.sh --tickers AAPL,MSFT
#   scripts/run_backfill_alpaca_csv.sh --since 2020-01-01
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/backfill_alpaca_csv.py "$@"
