#!/usr/bin/env bash
# Pre-PUSH gate — so a push is known-clean, not hoped-clean. Closes the recurring
# failure of pushing without running the gate.
#
# Policy (operator 2026-06-04): FAST gate by default (ruff + manifest check +
# surface/doc sentinels — seconds); the FULL authoritative whole-suite gate
# (`python -m pytest -p no:xdist`, the gate of record per DEV_PIPELINE_STANDARD)
# runs ONLY when the push contains code changes (anything that isn't docs/ or a
# .md file). If the push range can't be determined, it runs FULL (fail safe).
#
# Wired via .pre-commit-config.yaml (stages: [pre-push]) + .git/hooks/pre-push.
# Bypass (emergency): STE_SKIP_PREPUSH=1 git push   (or git push --no-verify)
set -uo pipefail
cd "$(dirname "$0")/.."

if [[ "${STE_SKIP_PREPUSH:-}" == "1" ]]; then
  echo "run_prepush_gate: STE_SKIP_PREPUSH=1 — skipping the pre-push gate (explicit bypass)." >&2
  exit 0
fi

PY=".venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

# What's being pushed: commits ahead of the upstream (fall back to origin/main).
if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
  range='@{u}..HEAD'
else
  range='origin/main..HEAD'
fi
changed="$(git diff --name-only "$range" 2>/dev/null || true)"

# A "code change" = any changed path that is NOT under docs/ and not a .md file.
if [[ -z "$changed" ]]; then
  code_changed="UNKNOWN"   # range indeterminate -> fail safe to FULL
else
  code_changed="$(printf '%s\n' "$changed" | grep -vE '^docs/|\.md$' | head -1 || true)"
fi

if [[ -n "$code_changed" ]]; then
  echo "run_prepush_gate: code change detected (e.g. '${code_changed}') — running FULL whole-suite gate ($PY -m pytest -p no:xdist)..." >&2
  exec "$PY" -m pytest -p no:xdist -q
fi

echo "run_prepush_gate: docs/markdown-only push — running FAST gate (check_manifests + surface/doc sentinels)..." >&2
# NOTE: ruff is deliberately NOT run here. A docs/markdown-only push changes no
# Python, and a whole-tree `ruff check .` flags pre-existing lint in tracked
# files that CI tolerates via its dir-scoped invocation (false failures). Lint
# is CI's job + the commit-stage pre-commit ruff hook; the code-change FULL path
# above runs the authoritative pytest gate.
"$PY" scripts/check_manifests.py || { echo "run_prepush_gate: check_manifests failed." >&2; exit 1; }
"$PY" -m pytest -p no:xdist -q -k "present or documented or memory or todo or manifest or contract or invariant or audit"
rc=$?
# pytest exit 5 = no tests matched the fast filter — acceptable for the fast path.
if [[ $rc -ne 0 && $rc -ne 5 ]]; then
  echo "run_prepush_gate: fast sentinel gate failed (rc=$rc)." >&2
  exit $rc
fi
echo "run_prepush_gate: FAST gate passed." >&2
