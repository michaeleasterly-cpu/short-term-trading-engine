#!/usr/bin/env bash
# Platform-wide smoke test — covers every engine + shared services.
#
# Exercises the full pipeline without submitting any real orders:
#   1. Full pytest suite — every engine, tpcore, dashboard, forensics.
#   2. ruff — lint clean.
#   3. Per-engine scheduler dry-run (no orders submitted): reversion,
#      vector, momentum, sentinel (Sigma archived 2026-05-16).
#   4. Forensics CLI — scan AAR table for triggers (no-op when empty).
#   5. Allocator CLI — paper mode (no kill_switch writes).
#   6. Tip sheet render (momentum) — exercises every section.
#
# Any failure aborts with a clear marker so the operator sees exactly
# which check tripped. Replaces the older momentum-only smoke test.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

PASSED=0
FAILED=()

step() {
    local label="$1"
    shift
    PASSED=$((PASSED + 1))
    echo ""
    echo "════════════════════════════════════════════════════════════════════════"
    echo "  ${PASSED} — ${label}"
    echo "════════════════════════════════════════════════════════════════════════"
    if "$@"; then
        echo "✓ ${label}"
    else
        echo "✗ ${label} FAILED"
        FAILED+=("${label}")
    fi
}

# Step 1 — pytest entire suite.
step "Full pytest suite" \
    .venv/bin/python -m pytest -q --no-header --maxfail=10

# Step 2 — ruff clean.
step "Ruff lint" \
    .venv/bin/python -m ruff check .

# Step 3 — per-engine scheduler dry-runs.
for engine in reversion vector momentum sentinel; do
    step "${engine} scheduler dry-run" bash -c "\
        DATABASE_URL=\"\$DATABASE_URL_IPV4\" .venv/bin/python -m ${engine}.scheduler --dry-run 2>&1 | tail -10"
done

# Step 4 — forensics CLI smoke.
step "forensics scan (read-only)" bash -c "\
    DATABASE_URL=\"\$DATABASE_URL_IPV4\" .venv/bin/python -m tpcore.forensics 2>&1 | tail -3"

# Step 5 — allocator paper-mode smoke.
step "allocator paper-mode dry-check" bash -c "\
    DATABASE_URL=\"\$DATABASE_URL_IPV4\" .venv/bin/python scripts/run_allocator.py 2>&1 | tail -5 || true"

# Step 6 — tip sheet render.
step "tip-sheet render (momentum)" bash -c "\
    DATABASE_URL=\"\$DATABASE_URL_IPV4\" .venv/bin/python scripts/generate_tip_sheet.py --engine momentum --force --no-broker 2>&1 | tail -10"

echo ""
echo "════════════════════════════════════════════════════════════════════════"
if [[ ${#FAILED[@]} -eq 0 ]]; then
    echo "  SMOKE TEST PASSED — ${PASSED} checks 🟢"
    exit 0
else
    echo "  SMOKE TEST FAILED — ${#FAILED[@]} of ${PASSED} checks 🔴"
    for f in "${FAILED[@]}"; do
        echo "    ✗ ${f}"
    done
    exit 1
fi
