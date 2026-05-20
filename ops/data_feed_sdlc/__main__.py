"""DFCR CLI — ``python -m ops.data_feed_sdlc --dfcr <file>``.

Mirrors ``ops/engine_sdlc/__main__.py``: parse the DFCR file, classify,
validate, print the prepared transition, prompt the operator for the
binary ``APPROVE? (y/n)`` on ADD/REMOVE, apply on ``y`` (or apply
immediately on AUTOMATED — CUTOVER + cadence/threshold edits).

Non-interactive stdin fails closed (H-S3-7a-equivalent): operator
approval requires an actual TTY. The Carver-style PTY-fork wrapper
remains the path for operator-delegated automation.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import structlog

from ops.data_feed_sdlc.dfcr import DFCRAction, parse_dfcr
from ops.data_feed_sdlc.planner import (
    ApprovalClass,
    _read_bindings_snapshot,
    apply,
    classify,
    validate,
)

logger = structlog.get_logger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="data_feed_sdlc",
        description=(
            "Data Feed Change Request — prepare + validate + atomically "
            "apply a data-feed roster transition. ADD/REMOVE ask one "
            "binary y/n; MODIFY (CUTOVER + cadence/threshold) is "
            "automated-if-gated."
        ),
    )
    p.add_argument("--dfcr", required=True,
                   help="Path to a filled DFCR file.")
    return p.parse_args(argv)


def _read_confirm() -> str:
    """Read one line from the TTY. Non-interactive stdin / EOF raises
    so the caller fails closed (matches ECR planner discipline)."""
    if not sys.stdin.isatty():
        raise EOFError("non-interactive stdin")
    return input("APPROVE? (y/n) ").strip()


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(argv)
    text = Path(ns.dfcr).read_text()
    try:
        dfcr = parse_dfcr(text)
    except ValueError as exc:
        print(f"DFCR parse error: {exc}", file=sys.stderr)
        return 2

    snapshot = _read_bindings_snapshot()
    plan = classify(dfcr, snapshot)
    plan = validate(plan)

    if plan.rejection is not None:
        print(f"DFCR rejected on validation: {plan.rejection}", file=sys.stderr)
        return 1

    print("\n── Prepared transition ──")
    print(f"  operation : {plan.operation.value}")
    print(f"  feed      : {plan.feed}")
    print(f"  approval  : {plan.approval_class.value}")
    if plan.operation is DFCRAction.ADD:
        print(f"  kind      : {plan.kind}")
        print(f"  provider  : {plan.provider or '(derived)'}")
        print(f"  adapter   : {plan.adapter or '(derived)'}")
        if plan.derived_from:
            print(f"  upstream  : {plan.derived_from}")
        print(f"  cadence   : {plan.cadence or '(unspecified — defaults to daily)'}")
    elif plan.operation is DFCRAction.REMOVE:
        print(f"  reason    : {plan.reason or '(none)'}")
    elif plan.operation is DFCRAction.MODIFY:
        print(f"  change    : {plan.change}")
        print(f"  reason    : {plan.reason or '(none)'}")
    print()

    if plan.approval_class is ApprovalClass.AUTOMATED:
        # CUTOVER / cadence-or-threshold edits — apply immediately.
        res = apply(plan)
        if res.rejection is not None:
            print(f"apply rejected: {res.rejection}", file=sys.stderr)
            return 1
        print("APPLIED (automated, gated). Audit recorded.")
        print("Commit the working-tree change with normal git.")
        return 0

    # OPERATOR path — explicit binary, fail-closed.
    try:
        answer = _read_confirm()
    except EOFError:
        print(
            "declined (no interactive confirmation), nothing changed",
            file=sys.stderr,
        )
        return 1

    if answer not in ("y", "yes"):
        print(f"declined (answer={answer!r}), nothing changed", file=sys.stderr)
        return 1

    res = apply(plan)
    if res.rejection is not None:
        print(f"apply rejected: {res.rejection}", file=sys.stderr)
        return 1
    print("APPLIED. Audit recorded.")
    print("Commit the working-tree change with normal git.")
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI shim
    raise SystemExit(main())
