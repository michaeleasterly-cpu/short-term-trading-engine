"""SEC filings freshness check — confirms ``platform.sec_insider_transactions``
and ``platform.sec_material_events`` are staying current with the active
T1+T2 stock universe.

The standard data-adapter pipeline (Phase 2, 2026-05-14) requires every
ingest to have a validation check that exercises real shape — not just
"row count > 0". This check enforces:

* **Newest** filing across both tables is no older than ``MAX_AGE_DAYS``
  (default 14). Earnings cycles produce 8-Ks weekly, Form 4s ≥ daily —
  more than 2 weeks of no new filings across the entire T1+T2 stock
  set means the ingest is broken.
* **Coverage** — at least ``MIN_COVERAGE_PCT`` of T1+T2 stocks have at
  least one filing in the last ``COVERAGE_WINDOW_DAYS`` (default 180).
  Catches the case where the table looks fresh because *one* mega-cap
  ticker filed yesterday while the other 60 stocks haven't been
  touched in months.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "sec_filings_freshness"
MAX_AGE_DAYS = 14
MIN_COVERAGE_PCT = 0.30
COVERAGE_WINDOW_DAYS = 180


_FRESHNESS_SQL = f"""
    WITH addressable AS (
        SELECT lt.ticker
        FROM platform.liquidity_tiers lt
        LEFT JOIN platform.ticker_classifications tc USING (ticker)
        WHERE lt.tier <= 2
          AND COALESCE(tc.asset_class, 'stock') = 'stock'
    ),
    union_filings AS (
        SELECT ticker, filing_date FROM platform.sec_insider_transactions
        UNION ALL
        SELECT ticker, filing_date FROM platform.sec_material_events
    )
    SELECT
        (SELECT MAX(filing_date) FROM union_filings) AS newest_filing,
        (SELECT COUNT(*) FROM addressable) AS addressable_count,
        (SELECT COUNT(DISTINCT a.ticker)
         FROM addressable a
         JOIN union_filings uf ON uf.ticker = a.ticker
         WHERE uf.filing_date >= CURRENT_DATE - INTERVAL '{COVERAGE_WINDOW_DAYS} days'
        ) AS covered_count,
        (SELECT COUNT(*) FROM platform.sec_insider_transactions) AS insider_rows,
        (SELECT COUNT(*) FROM platform.sec_material_events) AS material_rows
"""


async def check_sec_filings_freshness(
    pool: asyncpg.Pool,
    source: Any = None,
) -> CheckResult:
    """Verify SEC filings tables are fresh + cover T1+T2 adequately."""
    del source
    started = time.perf_counter()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_FRESHNESS_SQL)

    newest = row["newest_filing"] if row else None
    addressable = int(row["addressable_count"] or 0) if row else 0
    covered = int(row["covered_count"] or 0) if row else 0
    insider_rows = int(row["insider_rows"] or 0) if row else 0
    material_rows = int(row["material_rows"] or 0) if row else 0

    failures: list[FailureDetail] = []

    from datetime import UTC, datetime
    today = datetime.now(UTC).date()

    if newest is None:
        failures.append(FailureDetail(
            ticker="<tables>",
            reason="empty_tables",
            expected=(
                f"sec_insider_transactions + sec_material_events populated "
                f"with at least one filing ≤ {MAX_AGE_DAYS}d old"
            ),
            observed=(
                f"both tables empty (insider_rows={insider_rows}, "
                f"material_rows={material_rows})"
            ),
        ))
    else:
        age_days = (today - newest).days
        if age_days > MAX_AGE_DAYS:
            failures.append(FailureDetail(
                ticker="<freshness>",
                reason="stale_newest_filing",
                expected=f"newest filing_date within {MAX_AGE_DAYS}d (today={today})",
                observed=f"newest_filing_date={newest} ({age_days}d ago)",
            ))

    if addressable == 0:
        pass  # universe issue, not a SEC issue
    elif newest is not None:
        coverage_pct = covered / addressable
        if coverage_pct < MIN_COVERAGE_PCT:
            failures.append(FailureDetail(
                ticker="<coverage>",
                reason="insufficient_stock_coverage",
                expected=(
                    f"≥ {MIN_COVERAGE_PCT:.0%} of T1+T2 stocks "
                    f"({int(MIN_COVERAGE_PCT * addressable)} of {addressable}) "
                    f"with a filing in last {COVERAGE_WINDOW_DAYS}d"
                ),
                observed=(
                    f"only {covered}/{addressable} stocks ({coverage_pct:.1%}) "
                    f"have a filing in last {COVERAGE_WINDOW_DAYS}d "
                    f"(insider_rows={insider_rows}, material_rows={material_rows})"
                ),
            ))

    duration_ms = int((time.perf_counter() - started) * 1000)
    passed = len(failures) == 0
    return CheckResult(
        name=CHECK_NAME,
        passed=passed,
        total=1,
        failed=0 if passed else 1,
        duration_ms=duration_ms,
        failures=failures,
    )


__all__ = [
    "CHECK_NAME",
    "COVERAGE_WINDOW_DAYS",
    "MAX_AGE_DAYS",
    "MIN_COVERAGE_PCT",
    "check_sec_filings_freshness",
]
