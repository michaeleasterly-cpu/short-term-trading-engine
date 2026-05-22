#!/usr/bin/env bash
# Wrapper for scripts/probe_catalyst_pead_macro_expansion.py.
# PR C of the catalyst money-engine delivery (operator brief 2026-05-22).
#
# This wrapper RESOLVES to its own directory so it runs from whichever
# checkout it lives in (main, worktree, etc). The probe driver itself
# does not care about cwd — it uses absolute paths internally.
set -euo pipefail
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$( cd "${SCRIPT_DIR}/.." && pwd )"
cd "${REPO_ROOT}"
# Source .env from THIS checkout (each worktree has its own .env via
# .worktreeinclude per CLAUDE.md). Fallback to the shared repo .env
# if the worktree's .env is missing.
if [[ -f .env ]]; then
    source .env
else
    source /Users/michael/short-term-trading-engine/.env
fi
# Pooler-stable IPv4 host (Supabase txn-pooler) is required for long
# Lab runs; the canonical builder pins the safe statement_cache_size /
# jit settings on the URL. Transaction-mode pool (port 6543) avoids
# the 15-client session-mode cap that a heavy probe burns through.
export DATABASE_URL="$DATABASE_URL_IPV4"
# Ensure the repo root is on sys.path so `import catalyst.backtest`
# resolves (zsh strips PYTHONPATH inheritance in some shells).
export PYTHONPATH="${REPO_ROOT}:${PYTHONPATH:-}"
# Use the shared venv from the main checkout (the worktree does not
# carry its own venv — the main checkout's .venv is the canonical one).
PYBIN="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "${PYBIN}" ]]; then
    PYBIN="/Users/michael/short-term-trading-engine/.venv/bin/python"
fi
"${PYBIN}" scripts/probe_catalyst_pead_macro_expansion.py
