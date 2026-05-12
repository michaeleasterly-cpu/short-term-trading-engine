#!/usr/bin/env bash
# Vector parameter search. Uses 50 candidates × all 3 walk-forward windows
# (every candidate evaluated in every window) for cleaner OOS averaging
# than the 200-trial random-subsample design used for the first Sigma run.
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -u scripts/search_parameters.py \
  --engine vector \
  --trials 50 --per-window-trials 50 \
  --train-start 2018-01-01 --holdout-end 2023-12-31 \
  --final-holdout-start 2024-01-01 --final-holdout-end 2025-12-31
