#!/usr/bin/env bash
set -euo pipefail
cd /Users/michael/short-term-trading-engine
source .env
# Pooler-stable IPv4 host (Supabase txn-pooler) is required for long
# Lab runs; the canonical builder pins the safe statement_cache_size /
# jit settings on the URL.
export DATABASE_URL="$DATABASE_URL_IPV4"
# Ensure the repo root is on sys.path so `import catalyst.backtest`
# resolves (zsh strips PYTHONPATH inheritance in some shells).
export PYTHONPATH="/Users/michael/short-term-trading-engine:${PYTHONPATH:-}"
.venv/bin/python scripts/probe_catalyst_pead_expansion.py
