#!/usr/bin/env bash
# Probe Alpaca's IEX, SIP, and OTC feeds for a ticker set so we can
# tell "missing from IEX" from "actually delisted."
#
# Usage:
#   scripts/run_probe_alpaca_feed.sh ALOV,LPCV,PAAC,XBPEW
#   scripts/run_probe_alpaca_feed.sh ALOV --since 2026-04-15
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

.venv/bin/python scripts/probe_alpaca_feed.py "$@"
