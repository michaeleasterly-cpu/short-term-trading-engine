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
       vector → momentum → sentinel schedulers back-to-back. Each engine handles
       its own market-closed / no-rebalance / no-candidates gating.

Flags:
    ``--force``    bypass the market-closed pre-flight in ``ops.py
                   --update`` and ``run_all_engines.sh``.
    ``--dry-run``  non-destructive test mode: forwards ``--dry-run``
                   to ``ops.py --update`` (each stage runner returns
                   DRY_RUN without invoking the handler — no writes
                   to ``platform.prices_daily`` / fundamentals /
                   anywhere) AND skips the engine sweep entirely
                   (no orders submitted to Alpaca paper). The
                   canonical "verify the wire path works during
                   market hours" combination is
                   ``--dry-run --force`` — zero side effects on
                   ``prices_daily``, ``open_orders``, ``aar_events``,
                   or Alpaca.

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

    argv = sys.argv[1:]
    force_flag = ["--force"] if "--force" in argv else []
    dry_run = "--dry-run" in argv
    dry_run_flag = ["--dry-run"] if dry_run else []

    # Phase 1 — data ops via ops.py --update. The final stage of
    # _STAGE_SPECS is `forensics` (added 2026-05-15), so dossiers
    # are refreshed in-line. ``--dry-run`` is forwarded; the stage
    # runner returns DRY_RUN status for each stage without invoking
    # the handler, so no platform table is written.
    update_rc = await _run_step(
        "ops_update",
        [
            sys.executable, str(OPS_PY),
            "--update",
            "--source", "platform_pipeline",
            *dry_run_flag,
            *force_flag,
        ],
    )
    if update_rc != 0:
        logger.error(
            "platform_pipeline.update_failed_skipping_engines",
            returncode=update_rc,
        )
        return update_rc

    # Phase 2 — engine sweep. Skipped entirely under ``--dry-run`` so
    # no real paper orders go to Alpaca. The engine schedulers don't
    # all honor ``--dry-run`` themselves (momentum has it; the
    # per-trade engines don't), and even a dry-run scan in those
    # engines fires real broker.submit calls from inside the order
    # manager. Skipping at this layer is the safe, complete answer.
    if dry_run:
        logger.info(
            "platform_pipeline.engine_sweep_skipped_dry_run",
            note="no orders submitted to Alpaca under --dry-run",
        )
        logger.info("platform_pipeline.complete", dry_run=True)
        return 0

    # Each engine handles its own market-state gating (sigma/reversion/
    # vector via session_contains, momentum via is_rebalance_day +
    # force-rebalance override).
    engines_rc = await _run_step(
        "engine_sweep",
        ["bash", str(RUN_ALL_ENGINES), *force_flag],
    )
    if engines_rc != 0:
        logger.error("platform_pipeline.engines_failed", returncode=engines_rc)
        return engines_rc

    logger.info("platform_pipeline.complete", dry_run=False)
    return 0


def main() -> None:  # pragma: no cover - CLI shim
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
