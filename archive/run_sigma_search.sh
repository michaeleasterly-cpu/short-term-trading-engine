#!/usr/bin/env bash
# Runs the full 200-trial Sigma parameter search.
# Loads .env to get DATABASE_URL_IPV4, then invokes the orchestrator using
# the venv's Python. Stdout goes to stdout — wrap with nohup/redirect.
set -euo pipefail
cd "$(dirname "$0")/.."

# Source the .env so DATABASE_URL_IPV4 is available even when this script
# runs detached (nohup doesn't inherit interactive-shell exports).
set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/search_parameters.py \
  --engine sigma --trials 200 \
  --train-start 2018-01-01 --holdout-end 2023-12-31 \
  --final-holdout-start 2024-01-01 --final-holdout-end 2025-12-31
