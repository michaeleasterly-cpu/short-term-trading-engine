"""``python -m ops.lab`` — the canonical on-demand Engine Lab entrypoint
(Engine SDLC SP2 T10).

Operator-driven, a separate OS process: it builds a :class:`LabCandidate`,
runs the walk-forward Lab EXACTLY ONCE inside a :class:`LabContext` (the
enforced isolation contract — server read-only pool + the single
allowlisted RW credibility pool + the fail-closed reentrancy guard),
renders + writes the two-exit graduation dossier, prints the dossier path
+ verdict, and exits 0 on SURVIVED else 1.

NEVER wired into ``dispatch_once`` / ``engine_dispatch`` / ``engine_service``
or any daemon (§6 concurrency-with-live safety). No-DSN resolves to an
explicit non-zero rc + a logged error — never a silent 0 (the canary
``-m``-no-op lesson; mirrors ``ops.weekly_digest`` / ``ops.engine_ladder``
``_amain`` shape).

The engine packages are imported lazily inside ``ops.lab.run`` (legal in
``ops/`` — exempt from the ``check_imports`` tpcore∌engine scan, H-S2-1);
this module imports only ``ops.lab.*`` + ``tpcore.lab.*`` + stdlib, so
``import ops.lab.__main__`` does NOT eager-import any engine.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date

import structlog

from ops.lab.dossier import write_lab_dossier
from ops.lab.run import run_lab
from tpcore.lab.context import LabContext
from tpcore.lab.models import LabCandidate

logger = structlog.get_logger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m ops.lab",
        description="On-demand Engine Lab — walk-forward + held-back DSR "
                    "verdict + graduation dossier (SDLC SP2). Recommendation "
                    "only; SP2 never applies it.",
    )
    p.add_argument("--candidate", required=True,
                   help="Lab candidate name (validated [A-Za-z0-9_-]+).")
    p.add_argument("--target-engine", required=True,
                   choices=("reversion", "vector", "momentum"),
                   help="The existing engine whose backtest contract the "
                        "Lab exercises.")
    p.add_argument("--intent", required=True,
                   choices=("promote_new", "fold_existing"),
                   help="Recommended exit if the candidate SURVIVES.")
    p.add_argument("--param-overrides", default="{}",
                   help="JSON dict of param overrides (default '{}').")
    p.add_argument("--notes", default="",
                   help="Free-text note recorded on the LabCandidate.")
    p.add_argument("--db-url", default=None,
                   help="Postgres URL; defaults to $DATABASE_URL.")
    p.add_argument("--trials", type=int, default=40,
                   help="Total parameter combinations to pre-sample "
                        "(default 40 — a sane small on-demand default).")
    p.add_argument("--per-window-trials", type=int, default=20,
                   help="Random subsample evaluated per walk-forward window "
                        "(default 20).")
    p.add_argument("--seed", type=int, default=0,
                   help="Seed for the parameter sampler (reproducibility).")
    # Optional window args mirroring ops.lab.run._parse_args; sane defaults
    # match the run.py contract so a bare invocation is fully specified.
    p.add_argument("--train-start", type=date.fromisoformat,
                   default=date(2018, 1, 1))
    p.add_argument("--holdout-end", type=date.fromisoformat,
                   default=date(2023, 12, 31))
    p.add_argument("--final-holdout-start", type=date.fromisoformat,
                   default=date(2024, 1, 1))
    p.add_argument("--final-holdout-end", type=date.fromisoformat,
                   default=date(2025, 12, 31))
    p.add_argument("--walk-forward-step", type=int, default=365)
    p.add_argument("--train-years", type=int, default=3)
    p.add_argument("--holdout-years", type=int, default=1)
    p.add_argument("--dsr-threshold", type=float, default=0.95)
    p.add_argument("--credibility-threshold", type=int, default=60)
    p.add_argument("--universe-tier-max", type=int, default=None)
    p.add_argument("--output", default=None,
                   help="CSV destination for per-trial results "
                        "(default backtests/<engine>_search_results.csv).")
    return p.parse_args(argv)


def _run_args(ns: argparse.Namespace, dsn: str) -> argparse.Namespace:
    """Build the full ``ops.lab.run._parse_args``-shaped Namespace the
    walk-forward expects — every field supplied so ``_run_lab_core``
    needs no further resolution."""
    from pathlib import Path
    out = ns.output
    return argparse.Namespace(
        engine=ns.target_engine,
        trials=ns.trials,
        per_window_trials=ns.per_window_trials,
        train_start=ns.train_start,
        holdout_end=ns.holdout_end,
        final_holdout_start=ns.final_holdout_start,
        final_holdout_end=ns.final_holdout_end,
        walk_forward_step=ns.walk_forward_step,
        train_years=ns.train_years,
        holdout_years=ns.holdout_years,
        seed=ns.seed,
        output=Path(out) if out else None,
        database_url=dsn,
        dsr_threshold=ns.dsr_threshold,
        credibility_threshold=ns.credibility_threshold,
        universe_tier_max=ns.universe_tier_max,
    )


async def _amain(argv: list[str] | None = None) -> int:
    ns = _parse_args(argv)

    # Build + validate the candidate FIRST — a bad name (path traversal,
    # etc.) fails loud via the LabCandidate pattern, before any DB work.
    try:
        overrides = json.loads(ns.param_overrides)
        if not isinstance(overrides, dict):
            raise ValueError("--param-overrides must be a JSON object")
        candidate = LabCandidate(
            name=ns.candidate,
            target_engine=ns.target_engine,
            param_overrides=overrides,
            intent=ns.intent,
            notes=ns.notes,
        )
    except Exception as exc:  # noqa: BLE001 — surface any build failure loud
        logger.error("lab.bad_candidate", error=str(exc))
        print(f"invalid Lab candidate: {exc}", file=sys.stderr)
        return 1

    dsn = ns.db_url or os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("lab.no_dsn")
        print(
            "DATABASE_URL not set — pass --db-url or export DATABASE_URL. "
            "The Lab is on-demand and never runs without an explicit DSN.",
            file=sys.stderr,
        )
        return 1

    run_args = _run_args(ns, dsn)
    try:
        async with LabContext(db_url=dsn):
            result = await run_lab(run_args, candidate=candidate)
    except Exception as exc:  # noqa: BLE001 — explicit non-zero, never silent
        logger.error("lab.run_failed", error=str(exc))
        print(f"Lab run failed: {exc}", file=sys.stderr)
        return 1

    path = write_lab_dossier(result)
    print(f"\nLab dossier → {path}")
    print(f"VERDICT: {result.verdict}  "
          f"(DSR={result.dsr:.4f}  credibility={result.credibility_score}  "
          f"recommended_exit={result.recommended_exit})")
    return 0 if result.verdict == "SURVIVED" else 1


def main() -> None:  # pragma: no cover - CLI shim
    sys.exit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":  # pragma: no cover
    main()
