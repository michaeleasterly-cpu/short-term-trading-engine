#!/usr/bin/env bash
# Thin env wrapper around the CANONICAL daily_bars backfill (forks no
# ingest logic — just sets the IPv4 pooler URL + invokes ops.py).
#
# Repairs the 2026-05-15 partial ingest (506/~7,650 — flagged by
# validation.prices_daily_freshness coverage_collapse). NOTE: the
# bounded `repair_gaps` auto-heal path is structurally BLIND to a
# single-session coverage collapse (it derives targets from the
# prices_daily_COMPLETENESS invariant, which passed; the red is
# FRESHNESS/coverage_collapse). The canonical operator backfill is
# `force_refresh=true` (bypasses the already-ingested skip-fast).
# Bounded `lookback_days=4` brackets the 2026-05-15 hole with overlap
# insurance while keeping the pull small enough to clear the 3600s
# stage timeout via the multi-symbol chunked endpoint.
#
# universe=active is REQUIRED: the stage default (all_active discovery)
# applies a coarse min_price/min_volume gate that passed only 468/8300
# symbols on the first attempt (gap NOT fixed — verified 534/7650).
# 'active' re-pulls the KNOWN ~7,650 tickers already in prices_daily
# with no discovery coarse filter — the path that actually repairs a
# coverage collapse.
#
# end_offset_days=1 is REQUIRED on the 'active' path: it defaults to
# end=today, and Alpaca's SIP free tier returns 403 for end=today
# (verified — all 77 chunks 403'd). Shifting end back 1 day
# (→ 2026-05-16, historical) clears the 403; lookback_days=4 still
# brackets the 2026-05-15 hole. This is the documented backfill knob
# (see handle_daily_bars docstring).
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -u scripts/ops.py \
  --stage daily_bars --param force_refresh=true --param lookback_days=4 \
  --param universe=active --param end_offset_days=1
