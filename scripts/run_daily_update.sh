#!/usr/bin/env bash
# Daily data maintenance — runs the five-stage ops CLI to pull today's
# bars, corporate actions, fundamentals refresh, etc. Idempotent; safe to
# re-run if the first invocation was interrupted.
#
# Run AFTER 4 PM ET (1 PM PT) so today's closing bars are available from
# the data providers. Sigma/Reversion/Vector/Momentum all consume from
# platform.prices_daily; without this update they trade on stale prices.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/ops.py --update
