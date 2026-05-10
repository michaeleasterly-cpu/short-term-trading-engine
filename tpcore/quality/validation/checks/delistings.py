"""Delistings check — spec §3.1.

For each fixture entry, try the primary `ticker` first; if it doesn't
satisfy all four conditions, try each `alt_ticker` in turn. The check
passes for an event if **any** of the candidate symbols satisfies them all.
Failure is reported against the primary ticker.

The four conditions:
    1. ticker exists in `platform.prices_daily`
    2. at least one row has `delisted = true`
    3. `delisting_date` is non-null
    4. `delisting_date` and the last bar's date are both within ±5 trading
       days of the fixture's recorded `delisting_date`
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

from tpcore.calendar import trading_days_between
from tpcore.quality.validation.models import CheckResult, FailureDetail
from tpcore.quality.validation.sources.delistings import (
    DelistingEvent,
    DelistingsSource,
)

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "delistings"
DATE_TOLERANCE_DAYS = 5


async def check_delistings(pool: asyncpg.Pool, source: DelistingsSource) -> CheckResult:
    started = time.perf_counter()
    events = source.list_delistings()
    failures: list[FailureDetail] = []

    for event in events:
        candidates = [event.ticker, *event.alt_tickers]
        passed_for_event = False
        last_failure: FailureDetail | None = None
        for ticker in candidates:
            ok, detail = await _evaluate_candidate(pool, ticker, event)
            if ok:
                passed_for_event = True
                break
            last_failure = detail
        if not passed_for_event:
            assert last_failure is not None  # at least one candidate was tried
            # Always report against the primary ticker, regardless of which alt was last tried.
            failures.append(
                FailureDetail(
                    ticker=event.ticker,
                    reason=last_failure.reason,
                    expected=last_failure.expected,
                    observed=last_failure.observed,
                )
            )

    duration_ms = int((time.perf_counter() - started) * 1000)
    total = len(events)
    failed = len(failures)
    return CheckResult(
        name=CHECK_NAME,
        passed=failed == 0,
        total=total,
        failed=failed,
        duration_ms=duration_ms,
        failures=failures,
    )


async def _evaluate_candidate(
    pool, ticker: str, event: DelistingEvent
) -> tuple[bool, FailureDetail | None]:
    """Return (passed, failure_detail). On pass, failure_detail is None."""
    rows = await _fetch_meta(pool, ticker)
    if not rows:
        return False, FailureDetail(
            ticker=ticker, reason="missing", expected="present", observed="absent"
        )
    last_bar_date = max(r["date"] for r in rows)
    any_delisted = any(r["delisted"] for r in rows)
    delisting_dates = [r["delisting_date"] for r in rows if r["delisting_date"] is not None]
    if not any_delisted:
        return False, FailureDetail(
            ticker=ticker, reason="not_delisted", expected="delisted=true", observed="delisted=false"
        )
    if not delisting_dates:
        return False, FailureDetail(
            ticker=ticker,
            reason="delisting_date_null",
            expected="non-null delisting_date",
            observed="all rows have NULL",
        )
    observed_delisting = max(delisting_dates)
    drift = trading_days_between(observed_delisting, event.delisting_date)
    if drift > DATE_TOLERANCE_DAYS:
        return False, FailureDetail(
            ticker=ticker,
            reason="date_drift",
            expected=f"within {DATE_TOLERANCE_DAYS} td of {event.delisting_date.isoformat()}",
            observed=f"{observed_delisting.isoformat()} ({drift} td drift)",
        )
    last_bar_drift = trading_days_between(last_bar_date, event.delisting_date)
    if last_bar_drift > DATE_TOLERANCE_DAYS:
        return False, FailureDetail(
            ticker=ticker,
            reason="last_bar_stale",
            expected=f"last bar within {DATE_TOLERANCE_DAYS} td of {event.delisting_date.isoformat()}",
            observed=f"{last_bar_date.isoformat()} ({last_bar_drift} td drift)",
        )
    return True, None


async def _fetch_meta(pool, ticker: str) -> list[dict]:
    sql = """
        SELECT date, delisted, delisting_date
        FROM platform.prices_daily
        WHERE ticker = $1
    """
    async with pool.acquire() as conn:
        return await conn.fetch(sql, ticker)


__all__ = ["check_delistings", "CHECK_NAME", "DATE_TOLERANCE_DAYS"]
