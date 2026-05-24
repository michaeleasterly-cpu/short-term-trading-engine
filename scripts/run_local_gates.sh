#!/usr/bin/env bash
# Run the FULL local pre-push gate set in one command.
#
# Mirrors what CI runs (gitleaks + ruff + vulture + pytest + check_imports)
# so that any green local run is a high-confidence "CI will pass" signal.
# The recurring failure mode this prevents: shipping a PR with only
# pytest+ruff checked locally, then watching vulture or gitleaks turn red
# on CI (see operator memory: feedback_run_gates_locally_on_commit).
#
# Usage:
#   bash scripts/run_local_gates.sh             # all gates, fail-fast
#   bash scripts/run_local_gates.sh --no-pytest # skip pytest (faster iter)
#   bash scripts/run_local_gates.sh --quick     # ruff + vulture only (~3s)
#
# Exit code: 0 if all gates pass, 1 if any gate fails. Each gate's
# output is preserved so failure mode is obvious.

set -uo pipefail
cd "$(dirname "$0")/.."

SKIP_PYTEST=0
QUICK=0
for arg in "$@"; do
    case "$arg" in
        --no-pytest)  SKIP_PYTEST=1 ;;
        --quick)      QUICK=1; SKIP_PYTEST=1 ;;
        *)            echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

# Resolve venv python — works from main checkout OR a worktree under
# .claude/worktrees/ (worktrees don't have their own venv).
if [ -x ".venv/bin/python3" ]; then
    PY=".venv/bin"
elif [ -x "/Users/michael/short-term-trading-engine/.venv/bin/python3" ]; then
    PY="/Users/michael/short-term-trading-engine/.venv/bin"
else
    echo "FATAL: cannot find .venv/bin/python3" >&2
    exit 1
fi

FAILED=()

run_gate() {
    local name="$1"; shift
    echo
    echo "=== $name ==="
    if "$@"; then
        echo "✓ $name"
    else
        echo "✗ $name FAILED"
        FAILED+=("$name")
    fi
}

# 1. ruff (linting + import-order). Path list mirrors CI's
#    `.github/workflows/ci.yml` ruff step exactly — vulture_allowlist.py
#    is deliberately excluded (it contains intentionally-unused names).
run_gate "ruff" "$PY/ruff" check \
    reversion/ vector/ momentum/ sentinel/ canary/ catalyst/ \
    tpcore/ scripts/ ops/

# 2. vulture (dead-code fail-on-new — matches CI gate at min-confidence 60).
if [ "$QUICK" -eq 1 ] || [ "$SKIP_PYTEST" -eq 0 ]; then
    run_gate "vulture" "$PY/vulture" --min-confidence 60 \
        tpcore ops reversion vector momentum sentinel canary catalyst \
        dashboard_components vulture_allowlist.py
fi

# 3. check_imports (basic AST parse to catch SyntaxError fast).
if [ "$QUICK" -eq 0 ]; then
    run_gate "check_imports" "$PY/python3" -c "
import ast, pathlib, sys
errors = []
for p in pathlib.Path('.').rglob('*.py'):
    if '.venv' in p.parts or '.claude' in p.parts:
        continue
    try:
        ast.parse(p.read_text())
    except SyntaxError as e:
        errors.append(f'{p}: {e}')
if errors:
    for e in errors: print(e, file=sys.stderr)
    sys.exit(1)
print('OK')
"
fi

# 4. gitleaks (secret scan — both gitleaks CI checks run this).
if [ "$QUICK" -eq 0 ]; then
    if command -v gitleaks >/dev/null 2>&1; then
        run_gate "gitleaks" gitleaks detect --no-banner --no-git --source .
    else
        echo
        echo "=== gitleaks ==="
        echo "(skipped — gitleaks not installed; brew install gitleaks)"
    fi
fi

# 5. pytest (whole single-process suite — the authoritative gate per
#    .claude/rules/tests-and-ci.md and feedback_ops_package_shadow_full_suite_gate).
if [ "$SKIP_PYTEST" -eq 0 ]; then
    run_gate "pytest" "$PY/python3" -m pytest -p no:xdist -q --tb=short
fi

echo
if [ ${#FAILED[@]} -eq 0 ]; then
    echo "ALL GATES PASSED — safe to push."
    exit 0
else
    echo "${#FAILED[@]} GATE(S) FAILED: ${FAILED[*]}"
    echo "Fix locally before pushing. The CI gates run the same checks."
    exit 1
fi
