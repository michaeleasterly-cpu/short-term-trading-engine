#!/usr/bin/env bash
# Probe driver for the autonomous-finder candidate
# ``reversion_earnings_season_5d_range_normal`` under the post-2026-05-22
# partial-axis regime-filter engine enrichment.
#
# Locks reversion.backtest.LAB_TARGET.param_ranges to pin
# regime_filter_v1='trend_only' (most permissive variant) + signal_mode=
# 'price_z'; passes regime_target=968624efa259 via --param-overrides.
# Restores the in-tree LAB_TARGET after the run.
#
# Run from repo root. Requires DATABASE_URL_IPV4 in .env. NEVER auto-
# invoked from CI - this is operator-discretion-only post-PR-merge
# (ledger spend discipline).
set -euo pipefail
cd /Users/michael/short-term-trading-engine
source .env
# Pooler-stable IPv4 host (Supabase txn-pooler) is required for long
# Lab runs; the canonical builder pins the safe statement_cache_size /
# jit settings on the URL.
export DATABASE_URL="$DATABASE_URL_IPV4"
# Ensure the repo root is on sys.path so `import reversion.backtest`
# resolves.
export PYTHONPATH="/Users/michael/short-term-trading-engine:${PYTHONPATH:-}"
.venv/bin/python scripts/probe_reversion_partial_axis.py
