"""Macro indicators freshness check.

Verifies ``platform.macro_indicators`` is current across the five
canonical FRED series. Built 2026-05-14 as part of the FRED adapter.

Failure conditions:

* Table is empty (initial ingest never ran).
* Any expected indicator has zero observations.
* Any indicator's newest ``date`` is older than ``MAX_AGE_DAYS``
  (default 90). Macro data is slow-moving — quarterly + monthly series
  acceptably lag by 30-60 days, but anything > 90 days means the
  weekly refresh stage has been broken for multiple cycles.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "macro_indicators_freshness"
MAX_AGE_DAYS = 90
EXPECTED_INDICATORS: tuple[str, ...] = (
    "sahm_rule",
    "industrial_production",
    "initial_claims",
    "yield_curve",
    "credit_spread",
    "hy_spread",
    "vix",
)
# ``hy_spread`` (BAMLH0A0HYM2) was truncated by FRED 2026-05-15 to a
# rolling 3-year window. Its full pre-truncation history was recovered
# 2026-05-16 (eco-archive + Scribd gap, validated 772/772 exact —
# contiguous 1996→2026) and it was re-added to the active
# ``INDICATOR_SERIES``: FRED still serves the rolling window so the
# weekly stage keeps the tail fresh. It is therefore back in the
# freshness check. ``credit_spread`` (BAA10Y) remains the active
# Sentinel Bear-Score signal pending a separate deferred decision.


_SQL = """
    SELECT indicator, MAX(date) AS latest_date, COUNT(*) AS rows_total
    FROM platform.macro_indicators
    GROUP BY indicator
"""


async def check_macro_indicators_freshness(
    pool: asyncpg.Pool,
    source: Any = None,
) -> CheckResult:
    """Verify macro_indicators is fresh across all five FRED series."""
    del source
    started = time.perf_counter()
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SQL)

    failures: list[FailureDetail] = []

    from datetime import UTC, datetime
    today = datetime.now(UTC).date()
    by_indicator: dict[str, dict[str, Any]] = {
        r["indicator"]: {"latest_date": r["latest_date"], "rows_total": int(r["rows_total"] or 0)}
        for r in rows
    }

    if not by_indicator:
        failures.append(FailureDetail(
            ticker="<table>",
            reason="empty_table",
            expected="macro_indicators populated with all five FRED series",
            observed="table is empty (FRED adapter never ran successfully)",
        ))
    else:
        for indicator in EXPECTED_INDICATORS:
            info = by_indicator.get(indicator)
            if info is None:
                failures.append(FailureDetail(
                    ticker=indicator,
                    reason="missing_indicator",
                    expected=f"observations for {indicator}",
                    observed="no rows present in macro_indicators",
                ))
                continue
            if info["rows_total"] == 0:
                failures.append(FailureDetail(
                    ticker=indicator,
                    reason="zero_observations",
                    expected="≥ 1 observation",
                    observed=f"rows_total=0 for {indicator}",
                ))
                continue
            age_days = (today - info["latest_date"]).days
            if age_days > MAX_AGE_DAYS:
                failures.append(FailureDetail(
                    ticker=indicator,
                    reason="stale_indicator",
                    expected=f"newest observation within {MAX_AGE_DAYS}d",
                    observed=(
                        f"{indicator} latest={info['latest_date'].isoformat()} "
                        f"({age_days}d ago)"
                    ),
                ))

    duration_ms = int((time.perf_counter() - started) * 1000)
    passed = len(failures) == 0
    return CheckResult(
        name=CHECK_NAME,
        passed=passed,
        total=len(EXPECTED_INDICATORS),
        failed=len(failures),
        duration_ms=duration_ms,
        failures=failures,
    )


__all__ = [
    "CHECK_NAME",
    "EXPECTED_INDICATORS",
    "MAX_AGE_DAYS",
    "check_macro_indicators_freshness",
]
