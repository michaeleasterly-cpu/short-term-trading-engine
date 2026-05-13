#!/usr/bin/env bash
# Reversion + Vector follow-up after Sigma completed in the all_searches run.
# No `set -e` — FAILED verdicts exit 1 but shouldn't abort the sweep.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

for engine in reversion vector; do
  echo "════════════════════════════════════════════════════════════════════════"
  echo "  Starting ${engine} search ($(date +'%Y-%m-%d %H:%M:%S'))"
  echo "════════════════════════════════════════════════════════════════════════"
  DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -u scripts/search_parameters.py \
    --engine "$engine" \
    --trials 50 --per-window-trials 50 \
    --universe-tier-max 2 \
    --train-start 2018-01-01 --holdout-end 2023-12-31 \
    --final-holdout-start 2024-01-01 --final-holdout-end 2025-12-31 \
    --output "backtests/${engine}_search_results_t12.csv" || true
  echo ""
done
echo "════════════════════════════════════════════════════════════════════════"
echo "  Reversion + Vector complete ($(date +'%Y-%m-%d %H:%M:%S'))"
echo "════════════════════════════════════════════════════════════════════════"
