"""Production ``run_stage`` — invokes the CANONICAL ``ops.py --stage``.

The orchestrator must never reimplement ingestion or fork a script;
every actual repair / re-validation goes through the one canonical
parameterised stage entrypoint, exactly as the operator runs it by
hand. This module is the only place that shells out.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# tpcore/selfheal/runner.py → repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_OPS = _REPO_ROOT / "scripts" / "ops.py"


def make_canonical_runner(run_id: str):
    """Build a ``run_stage(stage, params) -> exit_code`` that runs
    ``python scripts/ops.py --stage <stage> [--param k=v ...] --force
    --run-id <run_id>`` in the repo root, inheriting the environment
    (DATABASE_URL et al). Bounded by ops.py's own per-stage timeout."""

    async def run_stage(stage: str, params: dict[str, str]) -> int:
        argv = [
            sys.executable, str(_OPS),
            "--stage", stage,
            "--force", "--run-id", run_id,
        ]
        for key, value in params.items():
            argv += ["--param", f"{key}={value}"]
        logger.info("selfheal.runner.exec", stage=stage, params=params)
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(_REPO_ROOT),
        )
        rc = await proc.wait()
        logger.info("selfheal.runner.done", stage=stage, rc=rc)
        return rc

    return run_stage


__all__ = ["make_canonical_runner"]
