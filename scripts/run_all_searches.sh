#!/usr/bin/env bash
# Sweep the per-trade engines back-to-back on the T1+T2 universe (1,281
# names). Sigma archived 2026-05-16 — see archive/sigma/EULOGY.md.
# Sequential so they don't fight for DB connections; ~5-15 min per engine.
# NOTE: no `-e`. The orchestrator exits 1 on a FAILED verdict, which is a
# normal/expected outcome — not a crash. `-e` would abort the sweep on the
# first FAILED engine and skip the rest.
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
    --output "backtests/${engine}_search_results_t12.csv"
  echo ""
done
echo "════════════════════════════════════════════════════════════════════════"
echo "  All three engines complete ($(date +'%Y-%m-%d %H:%M:%S'))"
echo "════════════════════════════════════════════════════════════════════════"
