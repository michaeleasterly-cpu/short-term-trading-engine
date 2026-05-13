#!/usr/bin/env bash
# One-shot momentum paper-trading kickoff. Forces a mid-month rebalance so
# the live engine starts collecting paper performance immediately rather
# than waiting for the next natural first-trading-day-of-month rebalance.
# After this kickoff, the operator schedules the scheduler to run daily; it
# will quietly no-op on non-rebalance days and fire naturally on the 1st.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -m momentum.scheduler --force-rebalance
