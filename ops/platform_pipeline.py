"""Platform-pipeline: data refresh → engine sweep → exit.

Single-process consolidation of the two-daemon local setup
(``data_operations`` cron + ``engine-service`` poller) for Railway
deployment. Eliminates the inter-daemon DATA_OPERATIONS_COMPLETE
polling dependency by running the stages sequentially in one process.

Sequence:
    1. ``ops.py --update --source=platform_pipeline`` — runs all 15
       update stages including the final ``forensics`` stage. Refuses
       during NYSE regular session (the underlying stages enforce
       that); pass ``--force`` to bypass.
    2. ``scripts/run_all_engines.sh`` — runs sigma → reversion →
       vector → momentum schedulers back-to-back. Each engine handles
       its own market-closed / no-rebalance / no-candidates gating.

Exit codes:
    * 0 — both phases succeeded.
    * N>0 — first phase that failed propagates its exit code. The
      engine sweep is NOT attempted if ``--update`` exited non-zero,
      since stale data would produce bad signals.

Local execution: the ``data_operations`` (cron) + ``engine-service``
(poller) launchd daemons remain the canonical Mac path. Use this
script only for Railway deployment or for an explicit one-shot
"run the whole platform now" sequence.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
OPS_PY = REPO_ROOT / "scripts" / "ops.py"
RUN_ALL_ENGINES = REPO_ROOT / "scripts" / "run_all_engines.sh"


async def _run_step(name: str, argv: list[str]) -> int:
    """Spawn ``argv``, stream stdout/stderr live, return the exit code.

    Live streaming (not capture) keeps Railway's log output current
    so the operator can watch progress and intervene if stuck.
    """
    logger.info("platform_pipeline.step_start", step=name, argv=argv)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(REPO_ROOT),
        env=os.environ,
        stdout=None,  # inherit — direct to our stdout
        stderr=None,
    )
    rc = await proc.wait()
    logger.info("platform_pipeline.step_done", step=name, returncode=rc)
    return rc


async def amain() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("platform_pipeline.no_dsn", note="set DATABASE_URL")
        return 1

    force_flag = ["--force"] if "--force" in sys.argv[1:] else []

    # Phase 1 — data ops via ops.py --update. The final stage of
    # _STAGE_SPECS is `forensics` (added 2026-05-15), so dossiers
    # are refreshed in-line.
    update_rc = await _run_step(
        "ops_update",
        [sys.executable, str(OPS_PY), "--update", "--source", "platform_pipeline", *force_flag],
    )
    if update_rc != 0:
        logger.error(
            "platform_pipeline.update_failed_skipping_engines",
            returncode=update_rc,
        )
        return update_rc

    # Phase 2 — engine sweep. Each engine handles its own market-state
    # gating (sigma/reversion/vector via session_contains, momentum via
    # is_rebalance_day + force-rebalance override).
    engines_rc = await _run_step(
        "engine_sweep",
        ["bash", str(RUN_ALL_ENGINES), *force_flag],
    )
    if engines_rc != 0:
        logger.error("platform_pipeline.engines_failed", returncode=engines_rc)
        return engines_rc

    logger.info("platform_pipeline.complete")
    return 0


def main() -> None:  # pragma: no cover - CLI shim
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
