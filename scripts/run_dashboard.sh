#!/usr/bin/env bash
# Launches the operator dashboard. Sources .env so DATABASE_URL is set,
# then runs Streamlit on localhost only (NEVER bind 0.0.0.0; the
# dashboard has no auth and exposes broker-action buttons).
#
# Open the browser tab Streamlit prints (usually http://localhost:8501).
# Stop with Ctrl+C in this terminal.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

# Map IPV4 → the canonical DATABASE_URL for the dashboard process.
export DATABASE_URL="${DATABASE_URL:-$DATABASE_URL_IPV4}"

exec .venv/bin/streamlit run dashboard.py --server.address=127.0.0.1 --server.headless=false
