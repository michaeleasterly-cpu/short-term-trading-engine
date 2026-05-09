"""Splits check — spec §3.3.

For every fixture entry, the post-adjusted close on ``split_date - 1``
should match the close on ``split_date``. With the ingestion's
``adjustment="all"`` setting, the ratio ``close[before] / close[after]``
must land in `[0.99, 1.01]`. A missed split produces a ratio near
``ratio_den / ratio_num`` (e.g. 0.25 for a 4:1) — orders of magnitude
outside the band.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from tpcore.quality.validation.models import CheckResult, FailureDetail
from tpcore.quality.validation.sources.splits import SplitEvent, SplitsSource

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "splits"
RATIO_MIN = Decimal("0.99")
RATIO_MAX = Decimal("1.01")


async def check_splits(pool: "asyncpg.Pool", source: SplitsSource) -> CheckResult:
    """Verify each fixture split has a near-1.0 close ratio across its day."""
    started = time.perf_counter()
    events = source.list_splits()
    failures: list[FailureDetail] = []

    for event in events:
        rows = await _fetch_bars(pool, event.ticker)
        by_date = {r["date"]: Decimal(str(r["close"])) for r in rows}
        before = _last_bar_strictly_before(by_date, event.split_date)
        after = by_date.get(event.split_date)
        if before is None or after is None:
            failures.append(
                FailureDetail(
                    ticker=event.ticker,
                    reason="missing",
                    expected=f"bars on {event.split_date} and the trading day before",
                    observed=f"have_before={before is not None} have_after={after is not None}",
                )
            )
            continue
        ratio = before / after
        if not (RATIO_MIN <= ratio <= RATIO_MAX):
            failures.append(
                FailureDetail(
                    ticker=event.ticker,
                    reason="ratio_off",
                    expected=f"[{RATIO_MIN}, {RATIO_MAX}]",
                    observed=str(ratio),
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


def _last_bar_strictly_before(by_date: dict, target) -> Decimal | None:
    """Closest bar date that's strictly before ``target``."""
    candidates = [d for d in by_date if d < target]
    if not candidates:
        return None
    return by_date[max(candidates)]


async def _fetch_bars(pool, ticker: str) -> list[dict]:
    sql = """
        SELECT date, close
        FROM platform.prices_daily
        WHERE ticker = $1
        ORDER BY date
    """
    async with pool.acquire() as conn:
        return await conn.fetch(sql, ticker)


__all__ = ["check_splits", "CHECK_NAME"]
