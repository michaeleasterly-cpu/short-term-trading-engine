#!/usr/bin/env bash
# Rebuild Phase-1.4 — CSV-first identity substrate seed + gate.
#
# Canonical sequence (docs/superpowers/specs/2026-06-05-identity-entity-model-delta.md):
#   1. scripts/rebuild_identity_seed.py        — load the survivorship-free
#      identity graph from the pre-wipe snapshot CSVs (TKR-14 ids verbatim),
#      resolve lifetime_start (kill the 1900-01-01 sentinel), rebuild the
#      stock/reit issuer satellite, run the 10-probe gate (reports 1 violation:
#      the snapshot's reused-ticker overlaps, fixed in step 2).
#   2. scripts/resolve_ticker_history_overlaps.py — resolve the cross-
#      classification ticker_history overlaps (bar-bearing entity wins, bar-less
#      artifacts clipped/dropped) and run the FULL 10-probe gate — exit 0 iff
#      the substrate is 100% green and ready for the Phase-2 child loads.
#
# Usage:
#   scripts/run_identity_seed.sh              # seed + resolve + gate
#   scripts/run_identity_seed.sh --dry-run    # seed dry-run only (no DB writes)
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a
export DATABASE_URL="${DATABASE_URL_IPV4:-${DATABASE_URL:-}}"

.venv/bin/python scripts/rebuild_identity_seed.py "$@"
# The seed's embedded gate flags the snapshot's reused-ticker overlaps; the
# resolver fixes them and emits the authoritative final gate verdict.
if [[ "${*}" != *--dry-run* ]]; then
    .venv/bin/python scripts/resolve_ticker_history_overlaps.py
fi
