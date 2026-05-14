#!/usr/bin/env bash
# One-shot wrapper: momentum scheduler with --force-rebalance.
#
# Used for travel-induced cadence shifts — when the operator can't be
# available at the natural first-trading-day-of-month rebalance window,
# the rebalance is shifted forward via a one-shot launchd agent that
# invokes this wrapper.
#
# Idempotent in the sense that re-running on the same calendar day won't
# duplicate the rebalance plan (the plan is bound to as_of=today's bars).
# Re-running across multiple days WILL re-rebalance; only invoke once.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

echo "════════════════════════════════════════════════════════════════════════"
echo "  MOMENTUM FORCE-REBALANCE — $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "════════════════════════════════════════════════════════════════════════"

exec env DATABASE_URL="${DATABASE_URL_IPV4:-$DATABASE_URL}" \
    .venv/bin/python -m momentum.scheduler --force-rebalance
