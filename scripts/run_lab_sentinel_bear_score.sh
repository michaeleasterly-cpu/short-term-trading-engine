#!/usr/bin/env bash
set -euo pipefail
cd /Users/michael/short-term-trading-engine
source .env
export DATABASE_URL="$DATABASE_URL_IPV4"
.venv/bin/python -m ops.lab \
  --candidate sentinel_bear_score \
  --target-engine sentinel \
  --intent fold_existing \
  --param-overrides '{"bear_score_mode": "graduated"}'
