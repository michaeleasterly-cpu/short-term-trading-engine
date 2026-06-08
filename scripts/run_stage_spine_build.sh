#!/usr/bin/env bash
# Clean-slate identity-spine build into staging + P1–P5 completeness gate.
#
# Rebuilds platform_stage_spine.{ticker_classifications,ticker_history} from
# scratch (SEC-first lifetimes, reused-ticker disjoint windows,
# asset_class SEC-authority) and runs the P1–P5 gate that PROVES every
# priced ticker resolves before any destructive live change. This is the
# make-it-work precondition of the data-foundation re-ingest
# (docs/superpowers/plans/2026-06-08-data-foundation-reingest-plan.md).
#
# Non-destructive to live platform.* core tables — it only writes the
# platform_stage_spine schema. The destructive cut (swap staged spine into
# live + child re-ingest) is a separate, operator-gated step.
#
# Usage:
#   scripts/run_stage_spine_build.sh              # full build + gate
#   scripts/run_stage_spine_build.sh --gate-only  # re-run the gate only
#
# Long-running (full SEC submissions read + universe build). Foreground OK.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/stage_spine_build.py "$@"
