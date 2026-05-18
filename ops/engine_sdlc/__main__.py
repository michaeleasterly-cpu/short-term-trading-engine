"""``python -m ops.engine_sdlc`` — the Engine Change Request CLI (SP3).

A separate OS process, operator-driven, NEVER wired into any daemon /
dispatch (parity with python -m ops.lab). parse → classify → validate →
render the prepared diff → (ADD/REMOVE) explicit binary TTY y/n,
fail-closed on anything else/EOF/non-TTY → apply; (MODIFY/promote)
automated apply + done-receipt. Every terminal outcome emits an
ENGINE_CHANGE_REQUEST audit. Explicit non-zero, never silent 0 (the
canary -m-no-op lesson; H-S3-12).

This module imports stdlib + ``structlog`` + the SP3 ``ops.engine_sdlc``
planner/ecr at module load — mirroring ``ops/lab/__main__.py``'s
module-top ``from ops.lab.run import run_lab``. The planner/ecr keep
EVERY engine import lazy/function-local (H-S3-10), so
``import ops.engine_sdlc.__main__`` STILL eager-imports ZERO engine
modules (proven by ``test_importing_main_does_not_eager_import_an_engine``).
The planner names are module attributes here so the T8 tests can
``monkeypatch.setattr(cli, "apply", …)`` — the patchable seam.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import structlog

from ops.engine_sdlc import planner
from ops.engine_sdlc.ecr import parse_ecr
from ops.engine_sdlc.planner import (
    ApprovalClass,
    apply,
    attach_ecr_context,
    classify,
    promote,
    validate,
)

logger = structlog.get_logger(__name__)


def _emit_audit(*args, **kwargs) -> None:
    """Thin pass-through to the planner's audit sink. Indirect on
    purpose: the T8 audit assertion patches the DEFINITION site
    ``ops.engine_sdlc.planner._emit_audit`` (the SP2 "patch where
    defined" lesson), so the CLI must resolve it at call time — never
    bind a stale reference at import."""
    planner._emit_audit(*args, **kwargs)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m ops.engine_sdlc",
        description="Engine Change Request — prepare + validate + "
                    "atomically apply an engine lifecycle transition "
                    "(SP3). ADD/REMOVE ask one binary y/n; MODIFY/promote "
                    "are automated-if-gated.")
    sub = p.add_argument_group("input")
    sub.add_argument("--ecr", help="Path to a filled ECR file.")
    sub.add_argument("--promote", metavar="ENGINE",
                     help="LAB→PAPER promote ENGINE (automated, gated).")
    if not argv:
        p.print_usage(sys.stderr)
        raise SystemExit(2)
    return p.parse_args(argv)


def _read_confirm() -> str:
    """Read one line from the TTY. Non-interactive stdin / EOF raises so
    the caller fails closed (H-S3-7a)."""
    if not sys.stdin.isatty():
        raise EOFError("non-interactive stdin")
    return input("APPROVE? (y/n) ").strip()


def _validate_for_cli(plan, ecr):
    return validate(plan, ecr=ecr)


async def _amain(argv: list[str]) -> int:
    ns = _parse_args(argv)

    if ns.promote:
        res = promote(ns.promote)
        if res.rejection is not None:
            print(f"promote refused: {res.rejection}", file=sys.stderr)
            return 1
        print(f"promoted {ns.promote}: LAB → PAPER (automated, gated). "
              f"Audit: ENGINE_CHANGE_REQUEST. Commit the working-tree "
              f"change with normal git.")
        return 0

    if not ns.ecr:
        print("either --ecr <file> or --promote <engine> is required",
              file=sys.stderr)
        return 1

    from tpcore.engine_profile import _PROFILE
    try:
        with open(ns.ecr) as fh:
            ecr = parse_ecr(fh.read())
    except FileNotFoundError as exc:
        logger.error("ecr.not_found", error=str(exc))
        print(f"ECR file not found: {ns.ecr}", file=sys.stderr)
        return 1
    except ValueError as exc:
        logger.error("ecr.parse_fail", error=str(exc))
        print(f"ECR parse failed: {exc}", file=sys.stderr)
        return 1

    snapshot = {k: p.lifecycle_state for k, p in _PROFILE.items()}
    plan = attach_ecr_context(classify(ecr, snapshot), ecr)
    if plan.rejection is not None:
        _emit_audit(ecr.engine, ecr.action.value, plan.from_state,
                    plan.to_state, plan.approval_class, "rejected",
                    plan.rejection)
        print(f"ECR rejected: {plan.rejection}", file=sys.stderr)
        return 1

    vplan = _validate_for_cli(plan, ecr)
    if vplan.rejection is not None:
        _emit_audit(ecr.engine, ecr.action.value, vplan.from_state,
                    vplan.to_state, vplan.approval_class, "rejected",
                    vplan.rejection)
        print(f"ECR rejected on validation: {vplan.rejection}",
              file=sys.stderr)
        return 1

    print(f"\n── Prepared transition ──\n"
          f"  action     : {ecr.action.name}\n"
          f"  engine     : {ecr.engine}\n"
          f"  {vplan.from_state} → {vplan.to_state}\n"
          f"  approval   : {vplan.approval_class}\n"
          f"  dry consistency run: GREEN\n")

    if vplan.approval_class == ApprovalClass.AUTOMATED:
        res = apply(vplan)
        if res.rejection is not None:
            print(f"apply rejected: {res.rejection}", file=sys.stderr)
            return 1
        print("APPLIED (automated, gated). Audit emitted. Commit the "
              "working-tree change with normal git.")
        return 0

    # OPERATOR path — explicit binary, fail-closed.
    try:
        answer = _read_confirm()
    except EOFError:
        _emit_audit(ecr.engine, ecr.action.value, vplan.from_state,
                    vplan.to_state, vplan.approval_class,
                    "operator_declined", "EOF / non-interactive stdin")
        print("declined (no interactive confirmation), nothing changed",
              file=sys.stderr)
        return 1
    if answer not in ("y", "yes"):
        _emit_audit(ecr.engine, ecr.action.value, vplan.from_state,
                    vplan.to_state, vplan.approval_class,
                    "operator_declined", f"answer={answer!r}")
        print("declined, nothing changed", file=sys.stderr)
        return 1

    res = apply(vplan)
    if res.rejection is not None:
        print(f"apply rejected: {res.rejection}", file=sys.stderr)
        return 1
    print("APPLIED. Audit emitted. Commit the working-tree change with "
          "normal git (the executor never runs git).")
    return 0


def main() -> None:  # pragma: no cover - CLI shim
    sys.exit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":  # pragma: no cover
    main()
