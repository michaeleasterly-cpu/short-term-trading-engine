#!/usr/bin/env bash
# Reconcile platform.open_orders against Alpaca's authoritative state.
# Use after engine crashes leave orphan pending rows (see YUMC 2026-05-12).
# Same reconcile path TradeMonitor runs on startup — invoked one-shot.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/reconcile_open_orders.py "$@"
