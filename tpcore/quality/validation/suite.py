"""Suite orchestrator — runs the three checks in parallel, persists results.

Per spec §5: builds the (default fixture-backed) sources if none injected,
gathers checks via `asyncio.gather`, writes one `DataQualityScore` per check
to `platform.data_quality_log`, and returns the aggregate `SuiteResult`.

Per-check exceptions are wrapped: the offending check returns
`passed=False` with `failures=[FailureDetail(reason="exception", ...)]` and
the suite continues. DB-write failures bubble up — see spec §6.
"""
from __future__ import annotations

import asyncio
import json
import time
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import structlog

from tpcore.quality.data_quality import DataQualityScore, DataQualityWriter

from .checks.constituent import (
    CHECK_NAME as CONSTITUENT_NAME,
)
from .checks.constituent import (
    check_constituent_snapshot,
)
from .checks.corporate_actions_integrity import CHECK_NAME as CA_INTEGRITY_NAME
from .checks.corporate_actions_integrity import check_corporate_actions_integrity
from .checks.delistings import CHECK_NAME as DELISTINGS_NAME
from .checks.delistings import check_delistings
from .checks.fundamentals_integrity import CHECK_NAME as FUND_INTEGRITY_NAME
from .checks.fundamentals_integrity import check_fundamentals_integrity
from .checks.row_integrity import CHECK_NAME as ROW_INTEGRITY_NAME
from .checks.row_integrity import check_row_integrity
from .checks.splits import CHECK_NAME as SPLITS_NAME
from .checks.splits import check_splits
from .models import CheckResult, FailureDetail, SuiteResult
from .sources.constituents import ConstituentSource, FixtureConstituentSource
from .sources.delistings import DelistingsSource, FixtureDelistingsSource
from .sources.splits import FixtureSplitsSource, SplitsSource

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


_CheckFn = Callable[..., Awaitable[CheckResult]]


async def run_suite(
    pool: asyncpg.Pool,
    *,
    delistings: DelistingsSource | None = None,
    constituents: ConstituentSource | None = None,
    splits: SplitsSource | None = None,
    writer: DataQualityWriter | None = None,
    run_id: UUID | None = None,
) -> SuiteResult:
    """Run the three validation checks and persist their results."""
    started_at = datetime.now(UTC)
    started_perf = time.perf_counter()
    rid = run_id or uuid4()

    # Build the three sources (this is where fixture-load failures surface).
    delistings = delistings or FixtureDelistingsSource()
    constituents = constituents or FixtureConstituentSource()
    splits = splits or FixtureSplitsSource()
    writer = writer or DataQualityWriter(pool)

    # Run the checks in parallel; each individual check is wrapped so an
    # unexpected exception turns into a failed CheckResult instead of
    # blowing up the whole suite. ``row_integrity`` has no fixture source
    # — it scans prices_daily directly; pass ``None`` for parity with the
    # other check signatures.
    delistings_task = _safe_run(DELISTINGS_NAME, check_delistings, pool, delistings)
    constituent_task = _safe_run(CONSTITUENT_NAME, check_constituent_snapshot, pool, constituents)
    splits_task = _safe_run(SPLITS_NAME, check_splits, pool, splits)
    row_integrity_task = _safe_run(ROW_INTEGRITY_NAME, check_row_integrity, pool, None)
    fund_integrity_task = _safe_run(FUND_INTEGRITY_NAME, check_fundamentals_integrity, pool, None)
    ca_integrity_task = _safe_run(CA_INTEGRITY_NAME, check_corporate_actions_integrity, pool, None)
    (
        delistings_result, constituent_result, splits_result,
        row_integrity_result, fund_integrity_result, ca_integrity_result,
    ) = await asyncio.gather(
        delistings_task, constituent_task, splits_task,
        row_integrity_task, fund_integrity_task, ca_integrity_task,
    )
    checks: list[CheckResult] = [
        delistings_result, constituent_result, splits_result,
        row_integrity_result, fund_integrity_result, ca_integrity_result,
    ]

    finished_at = datetime.now(UTC)
    suite_passed = all(c.passed for c in checks)

    # Persist each check as a DataQualityScore row.
    for check in checks:
        score = DataQualityScore(
            source=f"validation.{check.name}",
            timestamp=started_at,
            latency_ms=check.duration_ms,
            missing_bars=check.failed,
            stale=not check.passed,
            confidence=_confidence(check),
            notes=json.dumps([f.model_dump(mode="json") for f in check.failures]),
        )
        await writer.write(score)

    duration_ms = int((time.perf_counter() - started_perf) * 1000)
    logger.info(
        "tpcore.validation.run_done",
        run_id=str(rid),
        passed=suite_passed,
        duration_ms=duration_ms,
        checks={c.name: c.passed for c in checks},
    )
    return SuiteResult(
        run_id=rid,
        started_at=started_at,
        finished_at=finished_at,
        checks=checks,
        passed=suite_passed,
    )


async def _safe_run(
    name: str, fn: _CheckFn, pool: asyncpg.Pool, source
) -> CheckResult:
    started = time.perf_counter()
    try:
        return await fn(pool, source)
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.exception("tpcore.validation.check.exception", check=name, error=str(exc))
        return CheckResult(
            name=name,
            passed=False,
            total=0,
            failed=1,
            duration_ms=duration_ms,
            failures=[
                FailureDetail(
                    ticker="<n/a>",
                    reason="exception",
                    expected="successful run",
                    observed=traceback.format_exception_only(type(exc), exc)[-1].strip(),
                )
            ],
        )


def _confidence(check: CheckResult) -> Decimal:
    if check.total <= 0:
        return Decimal("0.000")
    passed_count = max(0, check.total - check.failed)
    # Fixed scale of 3 decimal places matches data_quality_log.confidence (NUMERIC(4,3)).
    return (Decimal(passed_count) / Decimal(check.total)).quantize(Decimal("0.001"))


__all__ = ["run_suite"]
