#!/usr/bin/env bash
# C0.5 — Operator wrapper for the manual Claude session/cost report.
# Mirrors scripts/run_weekly_digest.sh shape. Manual invocation only:
# no Docker, no railway up, no Anthropic API, no memstore writes,
# no DB writes, no daemon. See docs/CLAUDE_SESSION_OBSERVABILITY.md.
set -euo pipefail

cd "$(dirname "$0")/.."

# Pick the project venv python if present; fall back to python3 on
# PATH. Same convention as the existing scripts/run_*.sh wrappers.
if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="python3"
fi

exec "$PY" scripts/claude_session_report.py "$@"
