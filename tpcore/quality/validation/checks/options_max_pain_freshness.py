"""options_max_pain freshness — greeks.pro daily snapshot must be fresh.

greeks.pro free tier gives one tracked symbol's max-pain per day. This
check asserts the tracked symbol has a snapshot whose ``observed_date``
is within ``MAX_AGE_DAYS`` calendar days. It exercises *real shape*
(latest observed_date per symbol), not "row count > 0" — passing on
live data is the gate (data_adapter_pipeline.md stage 3).
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.feeds import freshness_max_age_days
from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "options_max_pain_freshness"
MAX_AGE_DAYS = freshness_max_age_days("greeks_max_pain", 7)  # single source of truth: tpcore.feeds profile
# Symbols the platform expects a daily max-pain snapshot for. Free tier
# tracks one; SPY is the platform's canonical macro/regime proxy.
EXPECTED_SYMBOLS: tuple[str, ...] = ("SPY",)

_SQL = """
    SELECT symbol, MAX(observed_date) AS latest
    FROM platform.options_max_pain
    WHERE symbol = ANY($1::text[])
    GROUP BY symbol
"""


async def check_options_max_pain_freshness(
    pool: asyncpg.Pool, source: Any = None,
) -> CheckResult:
    """Each expected symbol must have a max-pain snapshot ≤ MAX_AGE_DAYS old."""
    del source
    started = time.perf_counter()
    failures: list[FailureDetail] = []

    async with pool.acquire() as conn:
        rows = await conn.fetch(_SQL, list(EXPECTED_SYMBOLS))
    latest_by_symbol = {r["symbol"]: r["latest"] for r in rows}

    today = datetime.now(UTC).date()
    for sym in EXPECTED_SYMBOLS:
        latest = latest_by_symbol.get(sym)
        if latest is None:
            failures.append(FailureDetail(
                ticker=sym, reason="missing_symbol",
                expected=f"a max-pain snapshot within {MAX_AGE_DAYS}d",
                observed="zero rows in options_max_pain for this symbol",
            ))
            continue
        age = (today - latest).days
        if age > MAX_AGE_DAYS:
            failures.append(FailureDetail(
                ticker=sym, reason="stale",
                expected=f"observed_date within {MAX_AGE_DAYS}d",
                observed=f"latest {latest.isoformat()} ({age}d ago)",
            ))

    duration_ms = int((time.perf_counter() - started) * 1000)
    if failures:
        logger.warning(
            "tpcore.validation.options_max_pain.stale",
            failures=[f.ticker for f in failures],
        )
    return CheckResult(
        name=CHECK_NAME,
        passed=len(failures) == 0,
        total=len(EXPECTED_SYMBOLS),
        failed=len(failures),
        duration_ms=duration_ms,
        failures=failures,
    )


__all__ = ["CHECK_NAME", "EXPECTED_SYMBOLS", "check_options_max_pain_freshness"]
