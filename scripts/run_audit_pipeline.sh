#!/usr/bin/env bash
# Wrapper for scripts/audit_pipeline.py — the canonical pipeline audit.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
# shellcheck disable=SC2046
export $(grep -E '^DATABASE_URL_IPV4=' .env | xargs)
export DATABASE_URL="${DATABASE_URL_IPV4:?DATABASE_URL_IPV4 missing}"
"$REPO_ROOT/.venv/bin/python" scripts/audit_pipeline.py "$@"
