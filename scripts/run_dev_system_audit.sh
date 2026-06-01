#!/usr/bin/env bash
# S2 — read-only dev-system audit wrapper.
#
# Runs the Packet Void dev-system audit + manifest-linter against the
# STE working tree in REPORT_ONLY mode. The wrapper is advisory: any
# drift findings are printed and recorded with their tool exit codes,
# but the wrapper itself returns 0 so it can be invoked from CI, a
# pre-commit hook, or a manual operator session without ever red-
# flagging STE.
#
# The wrapper exits non-zero ONLY when the dev-system installation
# is missing or its scripts cannot be found — that is an operator-
# fixable infrastructure problem, not a drift signal.
#
# Authoritative cross-reference:
#   * Adoption plan: docs/superpowers/plans/2026-06-01-ste-round-trip-dev-system-adoption-plan.md
#   * Dev system:    https://github.com/michaeleasterly-cpu/packetvoid-dev-system
#
# Read-only invariants enforced by tests/test_dev_system_audit_wrapper.py:
#   * Never invokes bootstrap_project.py (which would render artifacts
#     into the target).
#   * Never invokes git push, gh pr merge, railway, docker, or any
#     mutation surface.
#   * Never calls the Anthropic API or any memstore endpoint.
#   * Never writes into STE outside the operator-reports directory
#     (which lives under .gitignore'd .operator/).

set -uo pipefail

# ─────────────────────────────────────────────────────────────────────
# Resolve paths
# ─────────────────────────────────────────────────────────────────────

# STE root (the repo this wrapper lives in). Derived from the wrapper's
# own location, not $PWD, so the operator can invoke from anywhere.
STE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Dev-system location. Operator may override via env var; default is
# the operator's known sibling checkout.
PACKETVOID_DEV_SYSTEM_DIR="${PACKETVOID_DEV_SYSTEM_DIR:-/Users/michael/packetvoid-dev-system}"

AUDIT_SCRIPT="$PACKETVOID_DEV_SYSTEM_DIR/devsystem/scripts/audit_project.py"
CHECK_SCRIPT="$PACKETVOID_DEV_SYSTEM_DIR/devsystem/scripts/check_manifests.py"

# Optional Python interpreter override; default to the project venv.
PY="${PY:-$STE_ROOT/.venv/bin/python3}"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3 || true)"
fi

# ─────────────────────────────────────────────────────────────────────
# Sanity: tooling present?
# ─────────────────────────────────────────────────────────────────────

if [ ! -f "$AUDIT_SCRIPT" ] || [ ! -f "$CHECK_SCRIPT" ]; then
  echo "NEEDS_OPERATOR_ACTION: dev-system scripts missing at $PACKETVOID_DEV_SYSTEM_DIR"
  echo "  expected: $AUDIT_SCRIPT"
  echo "  expected: $CHECK_SCRIPT"
  echo "  Either (a) clone packetvoid-dev-system to the default location,"
  echo "         (b) set PACKETVOID_DEV_SYSTEM_DIR to its checkout, or"
  echo "         (c) skip this wrapper."
  exit 2
fi

if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  echo "NEEDS_OPERATOR_ACTION: no python3 interpreter found"
  exit 2
fi

# ─────────────────────────────────────────────────────────────────────
# Banner
# ─────────────────────────────────────────────────────────────────────

echo "── Packet Void dev-system audit ─ REPORT_ONLY ──"
echo "  dev-system:  $PACKETVOID_DEV_SYSTEM_DIR"
echo "  target:      $STE_ROOT"
echo "  python:      $PY"
echo "  invocation:  read-only — no STE files are written, no API calls"
echo

# ─────────────────────────────────────────────────────────────────────
# audit_project (compares rendered baseline against STE tree)
# ─────────────────────────────────────────────────────────────────────

echo "── stage 1/2: audit_project.py --target-dir <STE>"
audit_rc=0
"$PY" "$AUDIT_SCRIPT" --target-dir "$STE_ROOT" || audit_rc=$?
echo "  exit: $audit_rc"
echo

# ─────────────────────────────────────────────────────────────────────
# check_manifests (lints the rendered .claude / .github surface)
# ─────────────────────────────────────────────────────────────────────

echo "── stage 2/2: check_manifests.py --target-dir <STE>"
check_rc=0
"$PY" "$CHECK_SCRIPT" --target-dir "$STE_ROOT" || check_rc=$?
echo "  exit: $check_rc"
echo

# ─────────────────────────────────────────────────────────────────────
# Verdict — advisory only, wrapper always returns 0
# ─────────────────────────────────────────────────────────────────────

if [ "$audit_rc" = "0" ] && [ "$check_rc" = "0" ]; then
  echo "REPORT_ONLY: CLEAN — STE is aligned with the dev-system baseline."
else
  echo "REPORT_ONLY: DRIFT_DETECTED"
  [ "$audit_rc" != "0" ] && echo "  audit_project       exit $audit_rc"
  [ "$check_rc" != "0" ] && echo "  check_manifests     exit $check_rc"
  echo "  This is advisory. STE CI is NOT failed by this wrapper."
  echo "  See docs/superpowers/plans/2026-06-01-ste-round-trip-dev-system-adoption-plan.md"
  echo "  for the per-artifact classification (PORTABLE_MATCH / STE_EXTENSION /"
  echo "  STE_OVERRIDE / CONFLICT / DEFER) and the staged adoption sequence."
fi
exit 0
