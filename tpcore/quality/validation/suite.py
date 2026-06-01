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

from .checks.aaii_sentiment_freshness import CHECK_NAME as AAII_SENTIMENT_NAME
from .checks.aaii_sentiment_freshness import check_aaii_sentiment_freshness
from .checks.borrow_rates_freshness import CHECK_NAME as BORROW_RATES_NAME
from .checks.borrow_rates_freshness import check_borrow_rates_freshness
from .checks.constituent import (
    CHECK_NAME as CONSTITUENT_NAME,
)
from .checks.constituent import (
    check_constituent_snapshot,
)
from .checks.corporate_actions_completeness import (
    CHECK_NAME as CA_COMPLETENESS_NAME,
)
from .checks.corporate_actions_completeness import (
    check_corporate_actions_completeness,
)
from .checks.corporate_actions_integrity import CHECK_NAME as CA_INTEGRITY_NAME
from .checks.corporate_actions_integrity import check_corporate_actions_integrity
from .checks.corporate_events_integrity import (
    CHECK_NAME as CORPORATE_EVENTS_INTEGRITY_NAME,
)
from .checks.corporate_events_integrity import check_corporate_events_integrity
from .checks.daemon_freshness import CHECK_NAME as DAEMON_FRESHNESS_NAME
from .checks.daemon_freshness import check_daemon_freshness
from .checks.data_operations_complete_cadence import (
    CHECK_NAME as DOC_CADENCE_NAME,
)
from .checks.data_operations_complete_cadence import (
    check_data_operations_complete_cadence,
)
from .checks.delistings import CHECK_NAME as DELISTINGS_NAME
from .checks.delistings import check_delistings
from .checks.earnings_events_freshness import CHECK_NAME as EARNINGS_EVENTS_NAME
from .checks.earnings_events_freshness import check_earnings_events_freshness
from .checks.earnings_events_monotone import (
    CHECK_NAME as EARNINGS_EVENTS_MONOTONE_NAME,
)
from .checks.earnings_events_monotone import check_earnings_events_monotone
from .checks.fear_greed_freshness import CHECK_NAME as FEAR_GREED_NAME
from .checks.fear_greed_freshness import check_fear_greed_freshness
from .checks.fundamentals_integrity import CHECK_NAME as FUND_INTEGRITY_NAME
from .checks.fundamentals_integrity import check_fundamentals_integrity
from .checks.fundamentals_quarterly_completeness import (
    CHECK_NAME as FUND_COMPLETENESS_NAME,
)
from .checks.fundamentals_quarterly_completeness import (
    check_fundamentals_quarterly_completeness,
)
from .checks.insider_sentiment_freshness import CHECK_NAME as INSIDER_SENTIMENT_NAME
from .checks.insider_sentiment_freshness import check_insider_sentiment_freshness
from .checks.issuer_history_integrity import (
    CHECK_NAME as ISSUER_HISTORY_INTEGRITY_NAME,
)
from .checks.issuer_history_integrity import check_issuer_history_integrity
from .checks.issuer_securities_integrity import (
    CHECK_NAME as ISSUER_SECURITIES_INTEGRITY_NAME,
)
from .checks.issuer_securities_integrity import check_issuer_securities_integrity
from .checks.liquidity_tiers_completeness import (
    CHECK_NAME as LIQUIDITY_COMPLETENESS_NAME,
)
from .checks.liquidity_tiers_completeness import (
    check_liquidity_tiers_completeness,
)
from .checks.liquidity_tiers_freshness import CHECK_NAME as LIQUIDITY_FRESHNESS_NAME
from .checks.liquidity_tiers_freshness import check_liquidity_tiers_freshness
from .checks.macro_indicators_completeness import (
    CHECK_NAME as MACRO_COMPLETENESS_NAME,
)
from .checks.macro_indicators_completeness import (
    check_macro_indicators_completeness,
)
from .checks.macro_indicators_freshness import CHECK_NAME as MACRO_FRESHNESS_NAME
from .checks.macro_indicators_freshness import check_macro_indicators_freshness
from .checks.prices_daily_classification_id_completeness import (
    CHECK_NAME as PRICES_CLASSIFICATION_ID_NAME,
)
from .checks.prices_daily_classification_id_completeness import (
    check_prices_daily_classification_id_completeness,
)
from .checks.prices_daily_completeness import CHECK_NAME as PRICES_COMPLETENESS_NAME
from .checks.prices_daily_completeness import check_prices_daily_completeness
from .checks.prices_daily_freshness import CHECK_NAME as PRICES_FRESHNESS_NAME
from .checks.prices_daily_freshness import check_prices_daily_freshness
from .checks.row_integrity import CHECK_NAME as ROW_INTEGRITY_NAME
from .checks.row_integrity import check_row_integrity
from .checks.sec_filings_freshness import CHECK_NAME as SEC_FRESHNESS_NAME
from .checks.sec_filings_freshness import check_sec_filings_freshness
from .checks.sec_insider_monotone import CHECK_NAME as SEC_INSIDER_MONOTONE_NAME
from .checks.sec_insider_monotone import check_sec_insider_monotone
from .checks.short_interest_freshness import CHECK_NAME as SHORT_INTEREST_NAME
from .checks.short_interest_freshness import check_short_interest_freshness
from .checks.social_sentiment_freshness import CHECK_NAME as SOCIAL_SENTIMENT_NAME
from .checks.social_sentiment_freshness import check_social_sentiment_freshness
from .checks.splits import CHECK_NAME as SPLITS_NAME
from .checks.splits import check_splits
from .checks.ticker_classifications_freshness import (
    CHECK_NAME as CLASSIFICATIONS_NAME,
)
from .checks.ticker_classifications_freshness import (
    check_ticker_classifications_coverage,
)
from .checks.ticker_history_integrity import (
    CHECK_NAME as TICKER_HISTORY_INTEGRITY_NAME,
)
from .checks.ticker_history_integrity import check_ticker_history_integrity
from .models import CheckResult, FailureDetail, SuiteResult
from .sources.constituents import ConstituentSource, FixtureConstituentSource
from .sources.delistings import DelistingsSource, FixtureDelistingsSource
from .sources.splits import FixtureSplitsSource, SplitsSource

# Single source of truth for "what checks are in the suite right now."
# `capital_gate.assert_passed` reads from this so any new check added
# below is automatically expected in the completeness gate. Audit-fix
# D3-1 (2026-05-14): replaces a hardcoded 3-name set that had drifted
# stale as the suite grew from 3 → 10 checks.
KNOWN_CHECK_NAMES: tuple[str, ...] = (
    DELISTINGS_NAME,
    CONSTITUENT_NAME,
    SPLITS_NAME,
    ROW_INTEGRITY_NAME,
    FUND_INTEGRITY_NAME,
    FUND_COMPLETENESS_NAME,
    CA_INTEGRITY_NAME,
    CA_COMPLETENESS_NAME,
    EARNINGS_EVENTS_NAME,
    EARNINGS_EVENTS_MONOTONE_NAME,
    SEC_FRESHNESS_NAME,
    SEC_INSIDER_MONOTONE_NAME,
    LIQUIDITY_FRESHNESS_NAME,
    LIQUIDITY_COMPLETENESS_NAME,
    CLASSIFICATIONS_NAME,
    MACRO_FRESHNESS_NAME,
    MACRO_COMPLETENESS_NAME,
    PRICES_FRESHNESS_NAME,
    PRICES_COMPLETENESS_NAME,
    PRICES_CLASSIFICATION_ID_NAME,
    INSIDER_SENTIMENT_NAME,
    SOCIAL_SENTIMENT_NAME,
    FEAR_GREED_NAME,
    SHORT_INTEREST_NAME,
    BORROW_RATES_NAME,
    AAII_SENTIMENT_NAME,
    ISSUER_HISTORY_INTEGRITY_NAME,
    ISSUER_SECURITIES_INTEGRITY_NAME,
    CORPORATE_EVENTS_INTEGRITY_NAME,
    TICKER_HISTORY_INTEGRITY_NAME,
    DAEMON_FRESHNESS_NAME,
    DOC_CADENCE_NAME,
)


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
    fund_completeness_task = _safe_run(
        FUND_COMPLETENESS_NAME, check_fundamentals_quarterly_completeness, pool, None
    )
    ca_integrity_task = _safe_run(CA_INTEGRITY_NAME, check_corporate_actions_integrity, pool, None)
    ca_completeness_task = _safe_run(
        CA_COMPLETENESS_NAME, check_corporate_actions_completeness, pool, None
    )
    earnings_events_task = _safe_run(EARNINGS_EVENTS_NAME, check_earnings_events_freshness, pool, None)
    earnings_events_monotone_task = _safe_run(
        EARNINGS_EVENTS_MONOTONE_NAME, check_earnings_events_monotone, pool, None
    )
    sec_task = _safe_run(SEC_FRESHNESS_NAME, check_sec_filings_freshness, pool, None)
    sec_insider_monotone_task = _safe_run(
        SEC_INSIDER_MONOTONE_NAME, check_sec_insider_monotone, pool, None
    )
    liquidity_task = _safe_run(
        LIQUIDITY_FRESHNESS_NAME, check_liquidity_tiers_freshness, pool, None
    )
    liquidity_completeness_task = _safe_run(
        LIQUIDITY_COMPLETENESS_NAME, check_liquidity_tiers_completeness, pool, None
    )
    classifications_task = _safe_run(
        CLASSIFICATIONS_NAME, check_ticker_classifications_coverage, pool, None
    )
    macro_task = _safe_run(
        MACRO_FRESHNESS_NAME, check_macro_indicators_freshness, pool, None
    )
    macro_completeness_task = _safe_run(
        MACRO_COMPLETENESS_NAME, check_macro_indicators_completeness, pool, None
    )
    prices_task = _safe_run(
        PRICES_FRESHNESS_NAME, check_prices_daily_freshness, pool, None
    )
    completeness_task = _safe_run(
        PRICES_COMPLETENESS_NAME, check_prices_daily_completeness, pool, None
    )
    classification_id_task = _safe_run(
        PRICES_CLASSIFICATION_ID_NAME,
        check_prices_daily_classification_id_completeness, pool, None
    )
    issuer_history_task = _safe_run(
        ISSUER_HISTORY_INTEGRITY_NAME,
        check_issuer_history_integrity, pool, None
    )
    issuer_securities_task = _safe_run(
        ISSUER_SECURITIES_INTEGRITY_NAME,
        check_issuer_securities_integrity, pool, None
    )
    corporate_events_task = _safe_run(
        CORPORATE_EVENTS_INTEGRITY_NAME,
        check_corporate_events_integrity, pool, None
    )
    ticker_history_task = _safe_run(
        TICKER_HISTORY_INTEGRITY_NAME,
        check_ticker_history_integrity, pool, None
    )
    insider_sentiment_task = _safe_run(
        INSIDER_SENTIMENT_NAME, check_insider_sentiment_freshness, pool, None
    )
    social_sentiment_task = _safe_run(
        SOCIAL_SENTIMENT_NAME, check_social_sentiment_freshness, pool, None
    )
    fear_greed_task = _safe_run(
        FEAR_GREED_NAME, check_fear_greed_freshness, pool, None
    )
    short_interest_task = _safe_run(
        SHORT_INTEREST_NAME, check_short_interest_freshness, pool, None
    )
    borrow_rates_task = _safe_run(
        BORROW_RATES_NAME, check_borrow_rates_freshness, pool, None
    )
    aaii_sentiment_task = _safe_run(
        AAII_SENTIMENT_NAME, check_aaii_sentiment_freshness, pool, None
    )
    daemon_freshness_task = _safe_run(
        DAEMON_FRESHNESS_NAME, check_daemon_freshness, pool, None,
    )
    doc_cadence_task = _safe_run(
        DOC_CADENCE_NAME, check_data_operations_complete_cadence, pool, None,
    )
    (
        delistings_result, constituent_result, splits_result,
        row_integrity_result, fund_integrity_result, fund_completeness_result, ca_integrity_result, ca_completeness_result,
        earnings_events_result, earnings_events_monotone_result,
        sec_result, sec_insider_monotone_result,
        liquidity_result, liquidity_completeness_result, classifications_result,
        macro_result, macro_completeness_result, prices_result, completeness_result,
        classification_id_result,
        insider_sentiment_result,
        social_sentiment_result, fear_greed_result,
        short_interest_result, borrow_rates_result,
        aaii_sentiment_result,
        issuer_history_result, issuer_securities_result,
        corporate_events_result, ticker_history_result,
        daemon_freshness_result, doc_cadence_result,
    ) = await asyncio.gather(
        delistings_task, constituent_task, splits_task,
        row_integrity_task, fund_integrity_task, fund_completeness_task, ca_integrity_task, ca_completeness_task,
        earnings_events_task, earnings_events_monotone_task,
        sec_task, sec_insider_monotone_task,
        liquidity_task, liquidity_completeness_task, classifications_task,
        macro_task, macro_completeness_task, prices_task, completeness_task,
        classification_id_task,
        insider_sentiment_task,
        social_sentiment_task, fear_greed_task,
        short_interest_task, borrow_rates_task,
        aaii_sentiment_task,
        issuer_history_task, issuer_securities_task,
        corporate_events_task, ticker_history_task,
        daemon_freshness_task, doc_cadence_task,
    )
    checks: list[CheckResult] = [
        delistings_result, constituent_result, splits_result,
        row_integrity_result, fund_integrity_result, fund_completeness_result, ca_integrity_result, ca_completeness_result,
        earnings_events_result, earnings_events_monotone_result,
        sec_result, sec_insider_monotone_result,
        liquidity_result, liquidity_completeness_result, classifications_result,
        macro_result, macro_completeness_result, prices_result, completeness_result,
        classification_id_result,
        insider_sentiment_result,
        social_sentiment_result, fear_greed_result,
        short_interest_result, borrow_rates_result,
        aaii_sentiment_result,
        issuer_history_result, issuer_securities_result,
        corporate_events_result, ticker_history_result,
        daemon_freshness_result, doc_cadence_result,
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
