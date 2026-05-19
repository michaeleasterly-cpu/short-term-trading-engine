#!/usr/bin/env bash
# PreToolUse(Bash) — block subset pytest selectors when the working tree
# has uncommitted ops/ changes. The `ops/*.py` ↔ `scripts/ops.py`
# package-shadow gotcha makes subset runs unrepresentative of CI; the
# authoritative gate is the whole-suite + order-flip.
# Allow with: CLAUDE_ALLOW_SUBSET=1
# Authoritative external: https://code.claude.com/docs/en/hooks-guide
# Project SoT: .claude/rules/tests-and-ci.md + feedback_ops_package_shadow_full_suite_gate.
set -euo pipefail

if [ "${CLAUDE_ALLOW_SUBSET:-}" = "1" ]; then
  exit 0
fi

input="$(cat)"
cmd="$(echo "$input" | jq -r '.tool_input.command // empty')"

# Match pytest invocations: `pytest`, `python -m pytest`, `.venv/bin/python -m pytest`.
if ! echo "$cmd" | grep -qE '(^|[^a-zA-Z0-9_-])(\.venv/bin/)?python[0-9.]*[[:space:]]+-m[[:space:]]+pytest|(^|[^a-zA-Z0-9_-])pytest([[:space:]]|$)'; then
  exit 0
fi

# Detect a subset selector: a positional arg that's a .py file / test-id /
# path under one of the engine/test/tpcore/etc dirs, OR `-k <expr>`,
# OR `--lf`/`--ff` (last-failed selectors).
is_subset=0
if echo "$cmd" | grep -qE '[[:space:]](-k|-K)[[:space:]]'; then
  is_subset=1
fi
if echo "$cmd" | grep -qE '[[:space:]](--lf|--ff|--last-failed|--failed-first)([[:space:]]|$)'; then
  is_subset=1
fi
if echo "$cmd" | grep -qE '[[:space:]](tests/|tpcore/|scripts/|ops/|reversion/|vector/|momentum/|sentinel/|canary/|catalyst/|sigma/)'; then
  is_subset=1
fi
if echo "$cmd" | grep -qE '[[:space:]][a-zA-Z][a-zA-Z0-9_/.-]+\.py(::[a-zA-Z_0-9]+)?([[:space:]]|$)'; then
  is_subset=1
fi

if [ "$is_subset" -eq 0 ]; then
  exit 0
fi

# Subset detected — is ops/ in the changed set?
if ! git -C "$(pwd)" diff --name-only HEAD 2>/dev/null | grep -q '^ops/'; then
  exit 0
fi

echo "BLOCK: subset pytest selector + uncommitted ops/ changes — the \`ops/*.py\` ↔ \`scripts/ops.py\` package-shadow gotcha." >&2
echo "Subset pytest runs are unrepresentative of CI; the authoritative gate is the whole-suite + order-flip." >&2
echo "  Run the whole suite:   .venv/bin/python -m pytest -p no:xdist -q" >&2
echo "  Or the fast parallel:   .venv/bin/python -m pytest -n auto --dist loadgroup -q" >&2
echo "If you've already confirmed CI green and need a one-off subset run: set CLAUDE_ALLOW_SUBSET=1 in the env." >&2
echo "See .claude/rules/tests-and-ci.md." >&2
exit 2
