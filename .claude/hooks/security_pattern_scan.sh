#!/usr/bin/env bash
# PostToolUse(Edit|Write|MultiEdit|NotebookEdit) — Layer 1 security
# pattern scan. Thin bash wrapper around security_pattern_scan.py so
# the hook integrates with STE's existing bash-script hook convention.
#
# Kill switch: STE_SECURITY_PATTERN_SCAN_DISABLE=1
#
# Vendored from anthropics/claude-code plugins/security-guidance per
# docs/audits/2026-06-03-vendor-vs-handrolled.md §2 + operator decision
# §9 #1 (Layer 1 only; defer Layers 2 + 3 until cost is measured).
#
# Authoritative external:
#   - https://code.claude.com/docs/en/hooks
#   - https://github.com/anthropics/claude-code
set -e

# Allow override via env, otherwise prefer the project venv's python
# (matches STE's tests-and-ci convention) then fall back to system.
PY="${STE_SECURITY_PATTERN_SCAN_PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x "${CLAUDE_PROJECT_DIR}/.venv/bin/python" ]; then
    PY="${CLAUDE_PROJECT_DIR}/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
  elif command -v python >/dev/null 2>&1; then
    PY="python"
  else
    # No python available — advisory hook, never block.
    exit 0
  fi
fi

exec "$PY" "$(dirname "$0")/security_pattern_scan.py"
