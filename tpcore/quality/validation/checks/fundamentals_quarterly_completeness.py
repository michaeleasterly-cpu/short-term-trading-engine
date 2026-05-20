"""fundamentals_quarterly completeness — per-ticker quarterly-gap invariant.

``fundamentals_integrity`` validates row-level NULL handling and the
basic shape of pb/de/revenue. It is structurally blind to *missing
quarters* in a ticker's filing history: a company that filed Q1+Q2+Q4
but skipped Q3 passes integrity (each present row is well-formed) while
the engines silently lose a quarter's signal.

This check closes that hole with a *physical-truth invariant*:

    For every T1/T2 stock that is currently live (most-recent filing
    within the last LIVE_WITHIN_DAYS), every consecutive pair of
    ``period_end_date`` rows within its active filing range must be
    spaced ≤ MAX_QUARTERLY_GAP_DAYS apart. Any gap > MAX_QUARTERLY_GAP_DAYS
    → one or more missing quarters → FAIL.

Why gap-based rather than calendar-anchored: company fiscal years are
NOT universally calendar-aligned (AAPL: Sep year-end; retailers: Jan;
ag/energy: Feb/Aug). A calendar-quarter expected set would false-fail
every non-calendar fiscal year. Gap-based detection is fiscal-year-
agnostic: only consecutive-gap days matter.

The threshold is math-derived, not tunable:

* Q4 (Oct-Dec) is the longest calendar quarter — 92 days.
* SEC 10-Q deadline is 40-45 days after period-end; companies file by
  then (gaps in ``period_end_date`` are bounded by quarter length, not
  by filing latency).
* ``MAX_QUARTERLY_GAP_DAYS = 100`` = 92 + 8-day slack. A gap > 100 days
  is GUARANTEED to span > 1 quarter — that is a missing quarter, not
  a late filer. Lowering false-fails edge cases; raising hides gaps.

Within those boundaries the invariant is absolute:

* Universe boundary (``tier <= 2 AND asset_class = 'stock'``) — same as
  prices_daily_completeness, symmetric on purpose.
* Liveness gate (most-recent filing within last 120 days) — a stock
  silent > 4 months is a halt/delist (different failure class).
* Active-range only (``[first_period_end, last_period_end]``) — pre-IPO
  and post-delisting quarters are never demanded. Only legitimate
  exclusion.

The healer ``compute_fundamentals_repair_targets`` calls the same
``_evaluate`` — detector and healer cannot disagree.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "fundamentals_quarterly_completeness"

# Universe boundary — identical to prices_daily_completeness so the
# completeness invariants speak about the same tradeable set.
TRADEABLE_TIER_MAX = 2

# A gap > 100 days between consecutive period_end_date rows means at
# least one quarter is missing (Q4=92 days max, +8-day slack for
# late-filed quarters; the SEC 10-Q deadline is 40-45 days). This is
# the math-derived bound, NOT a tunable tolerance.
MAX_QUARTERLY_GAP_DAYS = 100

# Liveness gate: a stock that hasn't filed for > LIVE_WITHIN_DAYS is a
# halt/delist/private (a different failure class), not a fundamentals-
# ingest gap. Excluded count surfaced in the pass summary so the
# exclusion cannot become a hiding place.
LIVE_WITHIN_DAYS = 120

# Failure list cap for log size; CheckResult.failed always carries the
# TRUE total count so confidence reflects reality.
MAX_REPORTED = 25

# Buffer added to the computed repair lookback so the targeted re-pull
# comfortably brackets the oldest missing quarter (filing-date math has
# month-end variance).
REPAIR_LOOKBACK_BUFFER_DAYS = 14


_FILING_DATES_SQL = """
    WITH liquid AS (
        SELECT lt.ticker
        FROM platform.liquidity_tiers lt
        JOIN platform.ticker_classifications tc ON tc.ticker = lt.ticker
        WHERE lt.tier <= $1 AND tc.asset_class = 'stock'
    )
    SELECT fq.ticker, fq.period_end_date
    FROM platform.fundamentals_quarterly fq
    JOIN liquid USING (ticker)
    WHERE fq.period_end_date IS NOT NULL
    ORDER BY fq.ticker, fq.period_end_date
"""


@dataclass(frozen=True)
class _Evaluation:
    """One completeness evaluation — shared by check + healer.

    Exactly one of ``sentinel`` or the gap fields is meaningful: if
    ``sentinel`` is set the others are zero/empty.
    """

    sentinel: FailureDetail | None
    evaluated: int
    excluded_dark: int
    # ticker → sorted list of inferred missing period_end_dates
    gaps: dict[str, list[date]]


def _infer_missing_period_ends(
    earlier: date, later: date,
) -> list[date]:
    """Given two consecutive present filings ~Nx quarters apart,
    return the inferred missing quarter-ends between them.

    Approximates by placing missing quarter-ends evenly between the
    two anchors at ~92-day intervals. The HEALER uses ONLY the
    earliest missing date (to set ``lookback_days``); the CHECK uses
    the count for logging. Exact calendar-quarter-snapping isn't
    needed — gaps are the signal, the inferred dates are advisory.
    """
    gap_days = (later - earlier).days
    if gap_days <= MAX_QUARTERLY_GAP_DAYS:
        return []
    # ~92-day quarters between earlier and later.
    n_missing = max(1, round(gap_days / 92.0) - 1)
    out: list[date] = []
    for i in range(1, n_missing + 1):
        # Even spacing — these are advisory anchors, not exact period_ends.
        offset = int(round(gap_days * i / (n_missing + 1)))
        out.append(earlier + timedelta(days=offset))
    return out


async def _evaluate(pool: asyncpg.Pool) -> _Evaluation:
    """Run the invariant once. Single source of truth for both
    ``check_fundamentals_quarterly_completeness`` (detection) and
    ``compute_fundamentals_repair_targets`` (healing) — they cannot
    disagree because they are the same code."""
    today = datetime.now(UTC).date()
    live_cutoff = today - timedelta(days=LIVE_WITHIN_DAYS)

    async with pool.acquire() as conn:
        rows = await conn.fetch(_FILING_DATES_SQL, TRADEABLE_TIER_MAX)

    if not rows:
        return _Evaluation(
            sentinel=FailureDetail(
                ticker="<universe>",
                reason="empty_liquid_universe",
                expected=(
                    f"tier≤{TRADEABLE_TIER_MAX} stock with fundamentals "
                    f"filings to exist"
                ),
                observed=(
                    "zero T1/T2 stock filings resolved — "
                    "fundamentals_quarterly empty or liquidity_tiers/"
                    "ticker_classifications stale"
                ),
            ),
            evaluated=0, excluded_dark=0, gaps={},
        )

    # Group filings by ticker (rows are pre-sorted by SQL).
    per_ticker: dict[str, list[date]] = {}
    for r in rows:
        per_ticker.setdefault(r["ticker"], []).append(r["period_end_date"])

    evaluated = 0
    excluded_dark = 0
    gaps: dict[str, list[date]] = {}
    for ticker, period_ends in per_ticker.items():
        if not period_ends:
            continue
        last_filed = period_ends[-1]
        if last_filed < live_cutoff:
            # Dark / delisted / private — different failure class.
            excluded_dark += 1
            continue

        evaluated += 1
        if len(period_ends) < 2:
            # Cannot detect a gap with a single filing — newly-listed
            # ticker, surfaced in `evaluated` count but no gap to flag.
            continue
        ticker_gaps: list[date] = []
        for i in range(1, len(period_ends)):
            earlier = period_ends[i - 1]
            later = period_ends[i]
            inferred = _infer_missing_period_ends(earlier, later)
            ticker_gaps.extend(inferred)
        if ticker_gaps:
            gaps[ticker] = sorted(ticker_gaps)

    return _Evaluation(
        sentinel=None,
        evaluated=evaluated,
        excluded_dark=excluded_dark,
        gaps=gaps,
    )


async def check_fundamentals_quarterly_completeness(
    pool: asyncpg.Pool,
    source: Any = None,
) -> CheckResult:
    """Zero-tolerance: every consecutive quarter present in every
    T1/T2 live stock's filing range."""
    del source
    started = time.perf_counter()
    ev = await _evaluate(pool)

    if ev.sentinel is not None:
        return CheckResult(
            name=CHECK_NAME, passed=False, total=0, failed=1,
            duration_ms=int((time.perf_counter() - started) * 1000),
            failures=[ev.sentinel],
        )

    failures: list[FailureDetail] = []
    for ticker, missing in sorted(ev.gaps.items()):
        shown = ", ".join(d.isoformat() for d in missing[:8])
        more = "" if len(missing) <= 8 else f" (+{len(missing) - 8} more)"
        failures.append(FailureDetail(
            ticker=ticker,
            reason="missing_quarter",
            expected=(
                f"no consecutive filing gap > {MAX_QUARTERLY_GAP_DAYS} days "
                f"in active range"
            ),
            observed=(
                f"{len(missing)} inferred missing quarter(s) at: {shown}{more}"
            ),
        ))

    total_failed = len(failures)
    if total_failed == 0:
        logger.info(
            "tpcore.validation.fundamentals_completeness.ok",
            evaluated=ev.evaluated, excluded_dark=ev.excluded_dark,
        )
    else:
        logger.warning(
            "tpcore.validation.fundamentals_completeness.gap",
            offending_tickers=total_failed,
            evaluated=ev.evaluated, excluded_dark=ev.excluded_dark,
        )

    return CheckResult(
        name=CHECK_NAME,
        passed=total_failed == 0,
        total=max(ev.evaluated, 1),
        failed=total_failed,
        duration_ms=int((time.perf_counter() - started) * 1000),
        failures=failures[:MAX_REPORTED],
    )


async def compute_fundamentals_repair_targets(
    pool: asyncpg.Pool,
) -> tuple[list[str], int]:
    """Targets for the bounded auto-heal: tickers with at least one
    inferred missing quarter + a ``lookback_days`` that brackets the
    oldest missing quarter.

    Returns ``([], 0)`` when nothing to repair OR when a structural
    sentinel is active — those are NOT bars-backfill-fixable, so the
    caller must escalate rather than run a pointless re-pull. Shares
    :func:`_evaluate` with the check; heal can never target a different
    set than the detector reports.
    """
    ev = await _evaluate(pool)
    if ev.sentinel is not None or not ev.gaps:
        return [], 0
    tickers = sorted(ev.gaps)
    oldest_missing = min(d for missing in ev.gaps.values() for d in missing)
    today = datetime.now(UTC).date()
    lookback_days = (today - oldest_missing).days + REPAIR_LOOKBACK_BUFFER_DAYS
    return tickers, lookback_days


__all__ = [
    "CHECK_NAME",
    "LIVE_WITHIN_DAYS",
    "MAX_QUARTERLY_GAP_DAYS",
    "TRADEABLE_TIER_MAX",
    "check_fundamentals_quarterly_completeness",
    "compute_fundamentals_repair_targets",
]
