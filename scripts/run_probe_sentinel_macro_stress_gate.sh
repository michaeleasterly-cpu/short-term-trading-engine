#!/usr/bin/env bash
# Probe driver for the autonomous-finder candidate
# ``sentinel_macro_stress_gate_v1`` under the post-2026-05-22
# multi-signal-count engine surface enrichment.
#
# Locks sentinel.backtest.LAB_TARGET.param_ranges to pin
# bear_score_mode='macro_stress_count' + activation_score_threshold=60;
# the macro_stress_signal_count choice and the four per-signal float
# thresholds stay in their declared ranges so the Lab samples within
# the hypothesis space. Restores the in-tree LAB_TARGET after the run.
#
# Run from repo root. Requires DATABASE_URL_IPV4 in .env. NEVER auto-
# invoked from CI - this is operator-discretion-only post-PR-merge
# (ledger spend discipline; cumulative lab_trial_ledger.sentinel is
# already non-zero from prior probes).
set -euo pipefail
cd /Users/michael/short-term-trading-engine
source .env
# Pooler-stable IPv4 host (Supabase txn-pooler) is required for long
# Lab runs; the canonical builder pins the safe statement_cache_size /
# jit settings on the URL.
export DATABASE_URL="$DATABASE_URL_IPV4"
# Ensure the repo root is on sys.path so `import sentinel.backtest`
# resolves.
export PYTHONPATH="/Users/michael/short-term-trading-engine:${PYTHONPATH:-}"
.venv/bin/python scripts/probe_sentinel_macro_stress_gate.py
