"""corporate_actions completeness — monotone-row-count + CSV-archive
shrinkage gate.

``corporate_actions_integrity`` validates row-level shape (null fields,
ratio bounds). It is structurally blind to *table shrinkage*: a vendor
truncation that drops 30% of the historical splits/dividends passes
integrity (every remaining row is well-formed) while the engines
silently lose historical split-adjustment data.

This check closes that hole with a *physical-truth invariant* that has
no tolerance knob:

    The live ``platform.corporate_actions`` row count must be ≥ the
    latest CSV-archive snapshot's row count. Any DB shrinkage relative
    to the archived snapshot → FAIL.

Why this is the right shape for corporate_actions specifically:

* Corporate actions are *historical events* — splits + dividends with
  fixed action_date in the past. Rows are never legitimately deleted
  (a 1998 dividend doesn't unhappen).
* A re-ingestion that yields fewer rows → vendor truncation / API
  contract change — exactly the BAMLH0A0HYM2 / Sigma 22-site-drift
  failure mode the lifecycle is designed to surface.
* The CSV archive is the authoritative on-disk record of the last
  known-good snapshot (``tpcore.ingestion.csv_archive``).
  Comparing live DB vs latest archive is the disk-based oracle the
  audit pipeline already trusts (``scripts/audit_data_pipeline.py``
  ``_detect_archive_shrinkage`` uses the same primitive).
* Zero-tolerance: ANY positive shrinkage fails. The 20% default
  ``shrinkage_threshold_pct`` in :func:`tpcore.ingestion.csv_archive.
  detect_shrinkage` is a WARN-band for in-ingestion logging; THIS
  check is the GATE-band — 0%, no slack.

The healer re-pulls via the canonical ``corporate_actions`` stage
(``scripts/ops.py --stage corporate_actions``); bounded by
``max_attempts=2`` in the HealSpec.

Within those boundaries the invariant is absolute — there is
deliberately no percentage knob, no recency window, no per-action-type
gate. Those are exactly the knobs that let a vendor truncation hide.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "corporate_actions_completeness"

# Canonical CSV-archive source name; matches the producer at
# tpcore/ingestion/handlers.py:221 + the audit pipeline at
# scripts/audit_data_pipeline.py:179.
ARCHIVE_SOURCE = "alpaca_corporate_actions"

# 0% tolerance — any positive shrinkage fails. This is the GATE band;
# the in-ingestion WARN band uses the detect_shrinkage default of 20%.
GATE_SHRINKAGE_THRESHOLD_PCT = 0.0


_LIVE_COUNT_SQL = "SELECT COUNT(*) AS n FROM platform.corporate_actions"


@dataclass(frozen=True)
class _Evaluation:
    """One completeness evaluation — shared by check + healer.

    Exactly one of ``sentinel`` (a structural failure that blocks
    verification entirely) or the shrinkage fields is meaningful: if
    ``sentinel`` is set the others are zero/empty.
    """

    sentinel: FailureDetail | None
    live_rows: int
    archived_rows: int
    archived_path: str
    shrinkage_pct: float


async def _evaluate(pool: asyncpg.Pool) -> _Evaluation:
    """Run the invariant once. Single source of truth for both
    ``check_corporate_actions_completeness`` (detection) and
    ``compute_corp_actions_repair_targets`` (healing)."""
    # Lazy import to avoid a hard ingestion-time dependency on
    # csv_archive at module-load (suite imports are cheap by design).
    from tpcore.ingestion.csv_archive import detect_shrinkage

    async with pool.acquire() as conn:
        row = await conn.fetchrow(_LIVE_COUNT_SQL)
    live_rows = int(row["n"] or 0)

    # detect_shrinkage compares current_rows to the previous archive
    # snapshot. We treat the live DB row count AS the "current"
    # snapshot — i.e. "does the live table have at least as many rows
    # as the last known-good archive?".
    report = detect_shrinkage(
        ARCHIVE_SOURCE,
        live_rows,
        shrinkage_threshold_pct=GATE_SHRINKAGE_THRESHOLD_PCT,
    )

    if report is None:
        # No prior archive snapshot to compare against. This is the
        # SENTINEL case from audit_data_pipeline: surfacing
        # "I checked nothing" stops a silent green on a live-money
        # data-integrity guardrail. Pre-baseline (no archives written
        # yet) is structurally distinct from "no shrinkage detected".
        return _Evaluation(
            sentinel=FailureDetail(
                ticker="<corporate_actions>",
                reason="no_prior_archive",
                expected=(
                    f"≥1 prior CSV-archive snapshot for source="
                    f"{ARCHIVE_SOURCE!r} to compare against"
                ),
                observed=(
                    "no prior archive found — initial ingest never ran, "
                    "or archive dir was purged. Cannot prove "
                    "non-shrinkage; run scripts/ops.py --stage "
                    "corporate_actions to write a baseline snapshot."
                ),
            ),
            live_rows=live_rows, archived_rows=0,
            archived_path="<none>", shrinkage_pct=0.0,
        )

    return _Evaluation(
        sentinel=None,
        live_rows=live_rows,
        archived_rows=report.previous_rows,
        archived_path=report.previous_archive,
        shrinkage_pct=report.shrinkage_pct,
    )


async def check_corporate_actions_completeness(
    pool: asyncpg.Pool,
    source: Any = None,
) -> CheckResult:
    """Zero-tolerance: live DB row count ≥ latest CSV archive's row
    count for ``alpaca_corporate_actions``."""
    del source
    started = time.perf_counter()
    ev = await _evaluate(pool)

    if ev.sentinel is not None:
        return CheckResult(
            name=CHECK_NAME, passed=False, total=0, failed=1,
            duration_ms=int((time.perf_counter() - started) * 1000),
            failures=[ev.sentinel],
        )

    # 0% threshold: ANY positive shrinkage_pct fails the gate.
    if ev.shrinkage_pct > GATE_SHRINKAGE_THRESHOLD_PCT:
        failure = FailureDetail(
            ticker="<corporate_actions>",
            reason="db_shrunk_vs_archive",
            expected=(
                f"live DB rows ≥ archived snapshot rows "
                f"({ev.archived_rows} from {ev.archived_path})"
            ),
            observed=(
                f"live DB has {ev.live_rows} rows — "
                f"{ev.shrinkage_pct * 100:.2f}% smaller than archive "
                f"({ev.archived_rows} rows). Vendor truncation or "
                f"deletion event — heal via canonical corporate_actions "
                f"stage."
            ),
        )
        logger.warning(
            "tpcore.validation.corp_actions_completeness.shrunk",
            live_rows=ev.live_rows, archived_rows=ev.archived_rows,
            shrinkage_pct=ev.shrinkage_pct,
        )
        return CheckResult(
            name=CHECK_NAME, passed=False, total=1, failed=1,
            duration_ms=int((time.perf_counter() - started) * 1000),
            failures=[failure],
        )

    logger.info(
        "tpcore.validation.corp_actions_completeness.ok",
        live_rows=ev.live_rows, archived_rows=ev.archived_rows,
    )
    return CheckResult(
        name=CHECK_NAME, passed=True, total=1, failed=0,
        duration_ms=int((time.perf_counter() - started) * 1000),
        failures=[],
    )


async def compute_corp_actions_repair_targets(
    pool: asyncpg.Pool,
) -> tuple[list[str], int]:
    """Targets for the bounded auto-heal.

    For corporate_actions, the canonical stage re-pulls the FULL
    universe (Alpaca's corp-actions API is a single bulk call per
    ticker-batch — no per-ticker subset semantic). The returned
    ``[]`` tickers list signals "full universe" to the orchestrator
    (the existing pattern for stages that don't accept a tickers
    param). ``lookback_days=0`` lets the stage use its built-in
    default.

    Returns ``([], 0)`` either on healable-shrinkage OR on the no-prior-
    archive sentinel — the orchestrator's bounded retry runs the stage
    once; if shrinkage persists, escalate. Same pattern as the
    macro_indicators heal.
    """
    ev = await _evaluate(pool)
    if ev.sentinel is not None:
        # No prior archive — cannot REPAIR (no oracle). The HealSpec
        # for this check is still healable=True so the canonical
        # `corporate_actions` stage runs and WRITES a baseline archive
        # on its next invocation, which is the genuine remediation.
        return [], 0
    if ev.shrinkage_pct > GATE_SHRINKAGE_THRESHOLD_PCT:
        return [], 0
    return [], 0


__all__ = [
    "ARCHIVE_SOURCE",
    "CHECK_NAME",
    "GATE_SHRINKAGE_THRESHOLD_PCT",
    "check_corporate_actions_completeness",
    "compute_corp_actions_repair_targets",
]
