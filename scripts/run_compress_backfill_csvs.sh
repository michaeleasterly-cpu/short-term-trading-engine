#!/usr/bin/env bash
# Compress all uncompressed .csv files under data/alpaca_backfill,
# data/fmp_backfill, data/corp_actions_backfill. Safe to re-run.
set -uo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python scripts/compress_backfill_csvs.py "$@"
