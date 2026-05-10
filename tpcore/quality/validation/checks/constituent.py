"""Constituent snapshot check — spec §3.2.

Two assertions:
* Every current S&P 500 ticker is present in `platform.prices_daily` and has
  at least one bar within the last 5 trading days from "now" (proves the
  daily ingestion is alive).
* Every recent removal is present, and — if `expect_delisted: true` — at
  least one row has `delisted = true`.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from tpcore.calendar import trading_days_between
from tpcore.quality.validation.models import CheckResult, FailureDetail
from tpcore.quality.validation.sources.constituents import ConstituentSource

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "constituent"
RECENT_TOLERANCE_DAYS = 5


async def check_constituent_snapshot(
    pool: asyncpg.Pool, source: ConstituentSource
) -> CheckResult:
    started = time.perf_counter()
    today = datetime.now(UTC).date()
    current = source.list_current_sp500()
    removals = source.list_recent_removals()
    all_tickers = list({*current, *(r.ticker for r in removals)})
    rows = await _fetch_meta(pool, all_tickers)

    by_ticker: dict[str, list[dict]] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r)

    failures: list[FailureDetail] = []

    for ticker in current:
        ticker_rows = by_ticker.get(ticker)
        if not ticker_rows:
            failures.append(
                FailureDetail(
                    ticker=ticker, reason="missing", expected="present", observed="absent"
                )
            )
            continue
        last_bar = max(r["date"] for r in ticker_rows)
        if trading_days_between(last_bar, today) > RECENT_TOLERANCE_DAYS:
            failures.append(
                FailureDetail(
                    ticker=ticker,
                    reason="stale",
                    expected=f"bar within {RECENT_TOLERANCE_DAYS} td of {today.isoformat()}",
                    observed=last_bar.isoformat(),
                )
            )

    for removal in removals:
        ticker_rows = by_ticker.get(removal.ticker)
        if not ticker_rows:
            failures.append(
                FailureDetail(
                    ticker=removal.ticker,
                    reason="missing",
                    expected="present (was removed from S&P 500)",
                    observed="absent",
                )
            )
            continue
        if removal.expect_delisted and not any(r["delisted"] for r in ticker_rows):
            failures.append(
                FailureDetail(
                    ticker=removal.ticker,
                    reason="not_delisted",
                    expected="delisted=true",
                    observed="delisted=false",
                )
            )

    duration_ms = int((time.perf_counter() - started) * 1000)
    total = len(current) + len(removals)
    failed = len(failures)
    return CheckResult(
        name=CHECK_NAME,
        passed=failed == 0,
        total=total,
        failed=failed,
        duration_ms=duration_ms,
        failures=failures,
    )


async def _fetch_meta(pool, tickers: list[str]) -> list[dict]:
    sql = """
        SELECT ticker, date, delisted
        FROM platform.prices_daily
        WHERE ticker = ANY($1)
    """
    async with pool.acquire() as conn:
        return await conn.fetch(sql, tickers)


__all__ = ["check_constituent_snapshot", "CHECK_NAME", "RECENT_TOLERANCE_DAYS"]
