#!/usr/bin/env bash
# Identity-first orchestrator (Plan 3 Phase 1.4) — runs the four identity
# stages IN ORDER then the BLOCKING identity gate:
#   universe_build → issuers_build → ticker_history_reuse_build →
#   issuer_securities_build → identity_gate
#
# This is the canonical entry the coordinator uses to build the identity
# substrate BEFORE any child load (prices / fundamentals / lifecycle).
#
# Defaults to dry_run=true (assemble + report counts, NO writes, gate
# skipped). For the LIVE build pass `--param dry_run=false`; the gate then
# runs and ABORTS on an inconsistent substrate.
#
# Usage:
#   scripts/run_identity_build.sh                       # dry run (preview)
#   scripts/run_identity_build.sh --param dry_run=false # LIVE build + gate
#   scripts/run_identity_build.sh --param dry_run=false --param chunk_size=20000
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/ops.py \
    --stage identity_build "$@"
