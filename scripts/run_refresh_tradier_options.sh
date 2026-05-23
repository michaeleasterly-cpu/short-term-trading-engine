#!/usr/bin/env bash
# Wrapper for scripts/refresh_tradier_options.py — DORMANT INFRASTRUCTURE.
#
# Pulls fresh options chains from Tradier for a tier-filtered universe
# and UPSERTs into platform.tradier_options_chains. See
# docs/runbooks/options-data-turn-on.md for when + why to run this.
#
# Default scope: T1+T2 stocks+ETFs (~2,085 tickers), 3-way concurrency,
# ~3.5h wall time. Override via TIER_MAX and CONCURRENCY env vars.
#
# Usage:
#   bash scripts/run_refresh_tradier_options.sh                 # default T1+T2
#   TIER_MAX=1 bash scripts/run_refresh_tradier_options.sh      # T1 only (~3h)
#   CONCURRENCY=5 bash scripts/run_refresh_tradier_options.sh   # faster but
#                                                                # more aggressive
#                                                                # on Tradier rate
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
set -a; source .env; set +a
: "${TIER_MAX:=2}"
: "${CONCURRENCY:=3}"
DATABASE_URL="$DATABASE_URL_IPV4" \
  TIER_MAX="$TIER_MAX" \
  CONCURRENCY="$CONCURRENCY" \
  .venv/bin/python scripts/refresh_tradier_options.py
