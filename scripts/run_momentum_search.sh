#!/usr/bin/env bash
# Momentum parameter search — same defaults as the verified Phase 1 run.
# Persists the held-back credibility rubric to platform.data_quality_log
# (commit cb53a53), so subsequent tip-sheet runs find a credibility row.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -u scripts/search_parameters.py \
  --engine momentum \
  --trials 50 --per-window-trials 50 \
  --universe-tier-max 2 \
  --train-start 2018-01-01 --holdout-end 2023-12-31 \
  --final-holdout-start 2024-01-01 --final-holdout-end 2025-12-31 \
  --output backtests/momentum_search_results_t12_v3.csv
