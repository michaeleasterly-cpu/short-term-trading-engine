#!/usr/bin/env bash
# End-to-end smoke test for the Momentum engine.
#
# Exercises the full pipeline without submitting any real orders:
# 1. Unit tests for all plugs (filters, drawdown breaker, sizing).
# 2. Dry-run of the scheduler with --force-rebalance — exercises setup
#    detection, lifecycle, execution-risk sizing, capital gate, and the
#    drawdown circuit breaker against the live database + paper broker.
# 3. Tip sheet render — exercises every section (header, credibility,
#    holdings, recommendations, signals, trades, disclaimer).
#
# Any failure aborts. Designed as the canonical 'did Phase 2.5 break
# anything?' check before kicking off real rebalances.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

echo "════════════════════════════════════════════════════════════════════════"
echo "  1/3 — momentum plug unit tests"
echo "════════════════════════════════════════════════════════════════════════"
.venv/bin/python -m pytest momentum/tests/ -x -q --no-header || exit 1

echo ""
echo "════════════════════════════════════════════════════════════════════════"
echo "  2/3 — scheduler dry-run (no orders submitted)"
echo "════════════════════════════════════════════════════════════════════════"
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -m momentum.scheduler \
  --dry-run --force-rebalance 2>&1 | tail -15 || exit 1

echo ""
echo "════════════════════════════════════════════════════════════════════════"
echo "  3/3 — tip sheet render"
echo "════════════════════════════════════════════════════════════════════════"
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/generate_tip_sheet.py \
  --engine momentum --force --no-broker 2>&1 | tail -20 || exit 1

echo ""
echo "════════════════════════════════════════════════════════════════════════"
echo "  SMOKE TEST PASSED"
echo "════════════════════════════════════════════════════════════════════════"
