#!/usr/bin/env bash
# Daily data maintenance — runs the 7-stage ops CLI:
#   daily_bars → corporate_actions → coverage_fill → fundamentals_refresh
#   → data_validation → universe_prescreener → universe_simulation
#
# Idempotent; safe to re-run if the first invocation was interrupted.
# Failed stages auto-retry once for transient errors (timeout, 429, etc.).
#
# Run AFTER 4 PM ET (1 PM PT) — ops.py refuses to run during the regular
# session (would corrupt today's row in prices_daily). Pass --force to
# bypass that guard.
#
# For a comprehensive post-close workflow (update + audit + verify +
# compress), use ``scripts/run_post_close.sh`` instead — that's the
# one-button operator command.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/ops.py --update "$@"
