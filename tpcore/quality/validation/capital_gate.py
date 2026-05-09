"""Engine graduation gate — `assert_passed` is called by Sigma + Reversion.

Per spec §5: the engines call this before "the graduation return path"
(i.e. before swapping from the pre-grad cap to live sizing). It raises
:class:`ValidationStaleError` if no recent suite run exists, or
:class:`ValidationFailedError` if the most recent run had any check
fail.

The lookup is intentionally narrow: query the most recent timestamp for
any ``validation.%`` source, then collect every row sharing that
timestamp (the suite writes the three sources with one shared
``started_at``). If fewer than the three expected sources are present at
that timestamp, the run was partial and is treated as a failure.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

EXPECTED_SOURCES = frozenset(
    {"validation.delistings", "validation.constituent", "validation.splits"}
)


class ValidationStaleError(RuntimeError):
    """No recent validation run within ``max_age_days``."""


class ValidationFailedError(RuntimeError):
    """The most recent validation run had at least one failing check."""


async def assert_passed(pool: "asyncpg.Pool", *, max_age_days: int = 7) -> None:
    """Raise if the most recent suite run isn't fresh enough or didn't fully pass."""
    rows = await _fetch_validation_rows(pool)
    if not rows:
        raise ValidationStaleError("no validation runs found in data_quality_log")

    latest_ts = max(r["timestamp"] for r in rows)
    age = datetime.now(UTC) - latest_ts
    if age > timedelta(days=max_age_days):
        raise ValidationStaleError(
            f"most recent validation run is {age.days} days old (max {max_age_days})"
        )

    latest_rows = [r for r in rows if r["timestamp"] == latest_ts]
    sources_present = {r["source"] for r in latest_rows}
    missing = EXPECTED_SOURCES - sources_present
    if missing:
        raise ValidationFailedError(
            f"most recent validation run is missing sources: {sorted(missing)}"
        )

    failed_sources = sorted(r["source"] for r in latest_rows if r["stale"])
    if failed_sources:
        raise ValidationFailedError(
            f"most recent validation run had failing checks: {failed_sources}"
        )

    logger.debug(
        "tpcore.validation.gate.passed",
        latest_ts=latest_ts.isoformat(),
        age_days=age.days,
    )


async def _fetch_validation_rows(pool: "asyncpg.Pool") -> list[dict]:
    sql = """
        SELECT source, timestamp, stale
        FROM platform.data_quality_log
        WHERE source LIKE 'validation.%'
        ORDER BY timestamp DESC
    """
    async with pool.acquire() as conn:
        return await conn.fetch(sql)


__all__ = [
    "assert_passed",
    "ValidationFailedError",
    "ValidationStaleError",
    "EXPECTED_SOURCES",
]
