#!/usr/bin/env bash
# Tip Sheet — private operator research tool. Wraps generate_tip_sheet.py
# with .env-sourcing + venv-python so paste-wrap issues don't break the
# command. Pass --engine, optionally --days / --since / --force.
#
# Examples:
#   scripts/run_tip_sheet.sh --engine momentum
#   scripts/run_tip_sheet.sh --engine momentum --force --days 7
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/generate_tip_sheet.py "$@"
