#!/usr/bin/env bash
# One-shot wrapper: dump current DB state to CSV-first baseline archives.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
# shellcheck disable=SC2046
export $(grep -E '^DATABASE_URL_IPV4=' .env | xargs)
export DATABASE_URL="${DATABASE_URL_IPV4:?DATABASE_URL_IPV4 missing}"
"$REPO_ROOT/.venv/bin/python" scripts/dump_baseline_archives.py
