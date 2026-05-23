#!/usr/bin/env bash
# Wrapper for scripts/backfill_country_from_fmp.py.
# One-shot backfill of platform.ticker_classifications.country from FMP profiles.
# Idempotent (re-run skips already-populated rows). ~12 minutes for 13,773 tickers.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
set -a; source .env; set +a
.venv/bin/python scripts/backfill_country_from_fmp.py
