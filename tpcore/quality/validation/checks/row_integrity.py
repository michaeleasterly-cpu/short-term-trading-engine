"""Row-integrity check — catches structural data issues in ``prices_daily``.

The other three validation checks (delistings, constituent, splits) verify
known-event coverage. This check looks at the *shape* of every row: prices
must be positive, ``high >= low``, ``volume >= 0``, and no future dates.
Symptoms of ingestion bugs, malformed source data, or schema drift would
all show up here before any engine starts trading against them.

Costs ~15s against a 20M-row Supabase Pro table. Acceptable for the
validation stage's once-per-update cadence; the dashboard's validation
fetch reads the pre-computed result, not the live scan.

Severity policy: any violation is a failure. We do NOT exempt
"known-bad" historical rows by ticker/date here — those should be
cleaned out of ``prices_daily`` rather than masked at the check layer.
The capped 50-row failure list keeps the persisted notes JSON bounded
even when something goes pathologically wrong.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "row_integrity"

# Cap the violations we hold in memory + persist to data_quality_log.notes.
# 50 is enough for the operator to spot a structural problem without
# producing a wall-of-text in the dashboard expander.
FAILURE_CAP = 50

# Hard ceiling on plausible per-share close. BRK.A is the canonical
# high (~$700K). 100M leaves 100x headroom for distant futures while
# still catching the $99-trillion scale corruption we've seen from the
# deprecated Tradier source. No allowlist.
_MAX_PLAUSIBLE_CLOSE = 100_000_000

# Single OHLC consistency predicate: high must dominate {open, close, low}
# and low must be dominated by {open, close, high}. Captures every form
# of "physically impossible bar" in two comparisons.
_VIOLATION_CASE = f"""
    CASE
        WHEN close IS NULL                                                THEN 'close_null'
        WHEN close <= 0                                                   THEN 'close_nonpositive'
        WHEN close > {_MAX_PLAUSIBLE_CLOSE}                               THEN 'close_implausible'
        WHEN open  IS NULL OR high IS NULL OR low IS NULL                 THEN 'ohl_null'
        WHEN high  <  GREATEST(open, close, low)                          THEN 'high_not_dominant'
        WHEN low   >  LEAST(open, close, high)                            THEN 'low_not_dominated'
        WHEN volume IS NULL                                               THEN 'volume_null'
        WHEN volume < 0                                                   THEN 'volume_negative'
        WHEN date  >  CURRENT_DATE                                        THEN 'future_date'
    END
"""

_INTEGRITY_PREDICATE = f"""
       close IS NULL OR close <= 0 OR close > {_MAX_PLAUSIBLE_CLOSE}
    OR open IS NULL OR high IS NULL OR low IS NULL
    OR high < GREATEST(open, close, low)
    OR low > LEAST(open, close, high)
    OR volume IS NULL OR volume < 0
    OR date > CURRENT_DATE
"""

_INTEGRITY_SQL = f"""
    SELECT ticker, date, close, high, low, volume, {_VIOLATION_CASE} AS violation
    FROM platform.prices_daily
    WHERE {_INTEGRITY_PREDICATE}
    ORDER BY date DESC, ticker
    LIMIT $1
"""

_INTEGRITY_COUNT_SQL = f"""
    SELECT COUNT(*) AS total
    FROM platform.prices_daily
    WHERE {_INTEGRITY_PREDICATE}
"""


async def check_row_integrity(
    pool: asyncpg.Pool,
    source: Any = None,  # signature parity with the other suite checks
) -> CheckResult:
    """Scan ``platform.prices_daily`` for structural anomalies."""
    del source  # unused; integrity checks have no fixture source
    started = time.perf_counter()
    async with pool.acquire() as conn:
        total = int(await conn.fetchval(_INTEGRITY_COUNT_SQL) or 0)
        rows = await conn.fetch(_INTEGRITY_SQL, FAILURE_CAP)

    failures: list[FailureDetail] = []
    for r in rows:
        ticker = r["ticker"]
        date_iso = r["date"].isoformat() if r["date"] else "?"
        violation = r["violation"] or "unknown"
        # Construct an "observed" string that captures the actual values
        # so the operator can see immediately whether it's a stray zero,
        # a flipped hi/lo, or a future date.
        observed_bits: list[str] = []
        if r["close"] is not None:
            observed_bits.append(f"close={r['close']}")
        if r["high"] is not None:
            observed_bits.append(f"high={r['high']}")
        if r["low"] is not None:
            observed_bits.append(f"low={r['low']}")
        if r["volume"] is not None:
            observed_bits.append(f"volume={r['volume']}")
        failures.append(
            FailureDetail(
                ticker=f"{ticker}@{date_iso}",
                reason=violation,
                expected="close>0, high>=low, volume>=0, date<=today",
                observed=", ".join(observed_bits) if observed_bits else "(all-null row)",
            )
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    capped = total > FAILURE_CAP
    if capped:
        logger.warning(
            "tpcore.validation.row_integrity.capped",
            total=total,
            shown=FAILURE_CAP,
        )
    # CheckResult.{total, failed} drive ``_confidence`` in suite.py:
    # confidence = (total-failed)/total. We want a clean pass/fail
    # signal — total=1 with failed=0 (pass) or failed=1 (any
    # violations). The actual violation count + capped flag goes into
    # the FailureDetail list, which the persisted notes JSON carries
    # downstream.
    return CheckResult(
        name=CHECK_NAME,
        passed=total == 0,
        total=1,
        failed=0 if total == 0 else 1,
        duration_ms=duration_ms,
        failures=failures,
    )


__all__ = ["check_row_integrity", "CHECK_NAME", "FAILURE_CAP"]
