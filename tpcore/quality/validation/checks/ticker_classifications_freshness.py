"""Ticker classifications coverage — zero-tolerance source-count drift
invariant (Path D, replaces the previous percentage-knob coverage check).

``platform.ticker_classifications`` is UPSTREAM-DERIVED from Alpaca's
``/v2/assets`` listing — Alpaca IS the asset-class source of truth that
defines what counts as "stock" vs "etf" vs "spac" vs "fund". A B-shaped
"active universe survives the cut" invariant (the liquidity_tiers
shape, just shipped in PR #183) would be circular here because
ticker_classifications IS the universe definition.

The correct invariant for an upstream-derived table is row-count-
equals-source at write time:

    On every classify_tickers refresh, Alpaca returned N assets and we
    wrote a snapshot row to ``platform.ticker_classifications_source_count``
    with ``source_count = N``. The live ``COUNT(*)`` on
    ``platform.ticker_classifications`` must equal the most recent
    snapshot's source_count. ANY drift = FAIL. Zero tolerance — no
    percentage knob.

Why this replaces the previous ``MIN_COVERAGE_PCT=0.90`` coverage
check:

* A percentage knob is exactly the kind of operator-fudgeable
  tolerance the autonomous-self-heal P1 work is designed to remove.
* "90% of the active prices_daily universe has a row in
  ticker_classifications" is the WRONG invariant for an upstream-
  derived table — ticker_classifications IS the source of truth that
  defines the universe; comparing it to prices_daily's universe is
  circular and silently green when both drift together.
* "Live count == source count" is structural physical truth that no
  re-ingest can fudge: either Alpaca said N and our table has N, or
  it doesn't.

Freshness as the floor (preserved from the legacy check):
``freshness_max_age_days("ticker_classifications", default=60)`` —
classifications are near-static, so 60d is the right ceiling. Read from
the FeedProfile so the SoT is centralized.

First-run behavior (no snapshot row yet) returns PASS + a notice. The
next classify_tickers run seeds the baseline; the run after that gates
against it. Same "first-run seed" pattern as the per-ticker monotone
checks (sec_insider_monotone, earnings_events_monotone).

Heal route: the canonical ``classify_tickers`` stage with
``skip_guard_days=0`` (force the bounded re-pull past the 30d
skip-guard). Bounded by ``max_attempts=2``.
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

CHECK_NAME = "ticker_classifications_coverage"
MAX_AGE_DAYS = freshness_max_age_days("ticker_classifications", 60)


# Read the most recent snapshot row + the live COUNT(*). One round-trip
# per probe — keep the check cheap. The LIMIT 1 on the snapshot table
# selects the newest-snapshot via the snapshot_at PK descending sort.
_LATEST_SNAPSHOT_SQL = """
    SELECT snapshot_at, source_count
    FROM platform.ticker_classifications_source_count
    ORDER BY snapshot_at DESC
    LIMIT 1
"""

_LIVE_COUNT_SQL = "SELECT COUNT(*) AS n FROM platform.ticker_classifications"


async def check_ticker_classifications_coverage(
    pool: asyncpg.Pool,
    source: Any = None,
) -> CheckResult:
    """Zero-tolerance: live ``COUNT(*)`` on ``ticker_classifications``
    must equal the most recent snapshot's ``source_count``, AND the
    snapshot itself must be within ``MAX_AGE_DAYS``.

    First-run case (no snapshot row) returns PASS + a notice — the next
    classify_tickers run seeds the baseline. Same bootstrap shape as the
    sec_insider_monotone / earnings_events_monotone first-run pattern.
    """
    del source
    started = time.perf_counter()
    async with pool.acquire() as conn:
        snapshot_row = await conn.fetchrow(_LATEST_SNAPSHOT_SQL)
        live_row = await conn.fetchrow(_LIVE_COUNT_SQL)

    live_count = int(live_row["n"] or 0) if live_row else 0

    if snapshot_row is None:
        # First-run seed pattern — the next classify_tickers run will
        # write the baseline. Until then we PASS with a notice; the
        # check is not enforced retroactively against zero baseline.
        logger.info(
            "tpcore.validation.ticker_classifications_coverage.first_run",
            live_count=live_count,
            note=(
                "no snapshot row yet — classify_tickers run will seed "
                "platform.ticker_classifications_source_count"
            ),
        )
        return CheckResult(
            name=CHECK_NAME,
            passed=True,
            total=1,
            failed=0,
            duration_ms=int((time.perf_counter() - started) * 1000),
            failures=[],
        )

    source_count = int(snapshot_row["source_count"] or 0)
    snapshot_at: datetime = snapshot_row["snapshot_at"]

    failures: list[FailureDetail] = []

    # Drift gate — the physical-truth invariant. No percentage knob.
    if live_count != source_count:
        failures.append(FailureDetail(
            ticker="<drift>",
            reason="source_count_drift",
            expected=(
                f"live COUNT(*)={source_count} (matches last "
                f"classify_tickers source snapshot)"
            ),
            observed=(
                f"live row count drifted from last sync snapshot: "
                f"live={live_count}, snapshot={source_count} "
                f"(delta={live_count - source_count}, snapshot_at="
                f"{snapshot_at.isoformat()}). Re-run classify_tickers "
                f"to re-sync."
            ),
        ))

    # Freshness floor (preserved from legacy check) — classifications
    # are near-static but a >60d-old snapshot suggests the monthly
    # refresh is broken.
    age_days = (datetime.now(UTC) - snapshot_at).days
    if age_days > MAX_AGE_DAYS:
        failures.append(FailureDetail(
            ticker="<freshness>",
            reason="stale_snapshot",
            expected=f"latest snapshot within {MAX_AGE_DAYS}d",
            observed=(
                f"snapshot_at={snapshot_at.isoformat()} ({age_days}d ago) "
                f"— classify_tickers monthly refresh appears broken"
            ),
        ))

    duration_ms = int((time.perf_counter() - started) * 1000)
    passed = len(failures) == 0
    if passed:
        logger.info(
            "tpcore.validation.ticker_classifications_coverage.ok",
            live_count=live_count,
            source_count=source_count,
            snapshot_age_days=age_days,
        )
    else:
        logger.warning(
            "tpcore.validation.ticker_classifications_coverage.failed",
            live_count=live_count,
            source_count=source_count,
            snapshot_age_days=age_days,
            failure_reasons=[f.reason for f in failures],
        )
    return CheckResult(
        name=CHECK_NAME,
        passed=passed,
        total=1,
        failed=0 if passed else len(failures),
        duration_ms=duration_ms,
        failures=failures,
    )


__all__ = [
    "CHECK_NAME",
    "MAX_AGE_DAYS",
    "check_ticker_classifications_coverage",
]
