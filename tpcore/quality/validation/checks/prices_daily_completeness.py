"""Prices-daily completeness — the ungameable zero-gap invariant.

``prices_daily_freshness`` answers "is the *newest* bar recent, and did
coverage collapse?". ``audit_pipeline.prices_daily_gaps`` answers "is
there a >7-day hole that *ended in the last 14 days*?". Both are useful
but both are *gameable / blind*: freshness is satisfied by a single
recent bar, and the audit gap check has a 14-day recency window and a
>7-day-run minimum — an old un-backfilled hole, or a single missing
session, on a liquid name is invisible to every existing check.

This check closes that hole with a *physical-truth invariant* that has
no tolerance knob and no recency window:

    For every genuinely-liquid tradeable common stock that is currently
    trading, there must be a bar for EVERY NYSE session in the recent
    window that falls within that ticker's own active date range.
    One missing (ticker, session) → the check FAILS.

Why each scoping clause is a principled invariant boundary and NOT a
tolerance knob that hides failures:

* **tier ≤ 2 AND asset_class = 'stock'** — the exact set the engines
  treat as tradeable (identical to ``audit_pipeline.prices_daily_gaps``,
  kept symmetric on purpose). Completeness over SPACs/funds the engines
  never touch is not the outcome being guaranteed.
* **60d avg volume ≥ 500,000** — this *defines* the set of names that
  demonstrably trade every NYSE session. A name averaging 500k+ shares
  a day does not skip trading days, so "a bar for every session" is a
  true physical invariant for it. Below that floor, missing sessions
  are real market sparsity (thin tier-1 names, post-IPO halts, imploding
  micro-caps) — including them would make the check *wrong*, not
  stricter. This is the same liquidity floor the audit settled on after
  the 2026-05-15 expert recalibration.
* **traded within the last 2 sessions** — a *liveness* gate, not a
  tolerance. A liquid name that has gone fully dark for 3+ sessions is
  a halt/delisting (a different failure class owned by ``delistings`` /
  the ``delist_stale`` stage), not a daily-bars ingest gap. The count
  of names excluded this way is reported in the pass summary so the
  exclusion can never become a hiding place.

Within those boundaries the invariant is absolute: expected sessions
come from ``tpcore.calendar`` (XNYS — physical exchange truth, not a
parameter), intersected with the ticker's own ``[MIN(date), MAX(date)]``
so pre-IPO history is never demanded. ``missing = expected − present``;
**any** non-empty ``missing`` fails the check. There is deliberately no
percentage, no count threshold, no recency window — those are exactly
the knobs that let a gap hide.

The window is bounded to the most recent ``WINDOW_SESSIONS`` NYSE
sessions: this is the data the engines trade on *now*, it keeps the
check fast, and it is sized so the auto-remediation backfill
(``ops.py --stage daily_bars --param lookback_days=…``) can actually
fill anything this surfaces. Deep-history completeness is the 4-phase
``audit_pipeline.py``'s job, not this hot-path gate.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from tpcore import calendar as cal
from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "prices_daily_completeness"

# Universe boundary — identical to audit_pipeline.prices_daily_gaps so
# the invariant gate and the heuristic audit speak about the same set.
TRADEABLE_TIER_MAX = 2
LIQUID_MIN_AVG_VOL_60D = 500_000

# Recent window the engines actually trade on. Sized so the
# auto-remediation backfill can fill anything surfaced here.
WINDOW_SESSIONS = 30

# Liveness gate: a liquid name silent for more than this many of the
# most-recent sessions is a halt/delist (handled elsewhere), not a
# daily-bars ingest gap. Excluded count is surfaced, never hidden.
LIVE_WITHIN_SESSIONS = 2

# Failure list is capped for log size; CheckResult.failed always carries
# the TRUE offending-ticker count so confidence reflects reality.
MAX_REPORTED = 25


_LIQUID_UNIVERSE_SQL = """
    WITH liquid AS (
        SELECT lt.ticker
        FROM platform.liquidity_tiers lt
        JOIN platform.ticker_classifications tc ON tc.ticker = lt.ticker
        WHERE lt.tier <= $1 AND tc.asset_class = 'stock'
    ),
    vol AS (
        SELECT pd.ticker, AVG(pd.volume) AS avg_vol_60d
        FROM platform.prices_daily pd
        JOIN liquid USING (ticker)
        WHERE pd.delisted = false
          AND pd.date >= CURRENT_DATE - INTERVAL '60 days'
        GROUP BY pd.ticker
        HAVING AVG(pd.volume) >= $2
    )
    SELECT
        pd.ticker,
        MIN(pd.date)                                         AS first_bar,
        MAX(pd.date)                                         AS last_bar,
        ARRAY_AGG(DISTINCT pd.date)
            FILTER (WHERE pd.date = ANY($3::date[]))          AS window_dates
    FROM platform.prices_daily pd
    JOIN vol USING (ticker)
    WHERE pd.delisted = false
      AND pd.date >= $4
    GROUP BY pd.ticker
"""


# Buffer added to the computed repair lookback so the targeted re-pull
# comfortably brackets the oldest missing session (calendar→Alpaca day
# math, weekends/holidays).
REPAIR_LOOKBACK_BUFFER_DAYS = 4


@dataclass(frozen=True)
class _Evaluation:
    """One completeness evaluation — shared by the check and the healer.

    Exactly one of ``sentinel`` (a structural failure that blocks
    verification entirely) or the gap fields is meaningful: if
    ``sentinel`` is set the others are zero/empty.
    """

    sentinel: FailureDetail | None
    evaluated: int
    excluded_dark: int
    window_start: date
    # ticker → sorted list of missing NYSE sessions (within active range)
    gaps: dict[str, list[date]]


async def _evaluate(pool: asyncpg.Pool) -> _Evaluation:
    """Run the invariant once. The single source of truth for both
    ``check_prices_daily_completeness`` (detection) and
    ``compute_gap_repair_targets`` (the auto-heal targeting) — detector
    and healer can never disagree because they are the same code."""
    today = datetime.now(UTC).date()
    # Pull a generous calendar span, then take the last WINDOW_SESSIONS
    # actual NYSE sessions. ~9 calendar weeks comfortably covers 30
    # sessions including holidays.
    span_start = today - timedelta(days=WINDOW_SESSIONS * 2 + 30)
    all_sessions = cal.sessions_in_range(span_start, today)
    if not all_sessions:
        return _Evaluation(
            sentinel=FailureDetail(
                ticker="<calendar>",
                reason="no_sessions",
                expected=f"≥1 NYSE session in last ~{WINDOW_SESSIONS} sessions",
                observed="tpcore.calendar returned no sessions — calendar broken",
            ),
            evaluated=0, excluded_dark=0, window_start=today, gaps={},
        )

    window_sessions = all_sessions[-WINDOW_SESSIONS:]
    window_set = set(window_sessions)
    window_start = window_sessions[0]
    # A name must have printed within the last LIVE_WITHIN_SESSIONS
    # sessions to be considered "currently trading".
    live_floor = window_sessions[-LIVE_WITHIN_SESSIONS]

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _LIQUID_UNIVERSE_SQL,
            TRADEABLE_TIER_MAX,
            LIQUID_MIN_AVG_VOL_60D,
            list(window_sessions),
            window_start,
        )

    if not rows:
        return _Evaluation(
            sentinel=FailureDetail(
                ticker="<universe>",
                reason="empty_liquid_universe",
                expected=(
                    f"tier≤{TRADEABLE_TIER_MAX} stock w/ 60d avg vol "
                    f"≥ {LIQUID_MIN_AVG_VOL_60D:,} to exist"
                ),
                observed=(
                    "zero liquid tradeable names resolved — liquidity_tiers/"
                    "ticker_classifications stale or prices_daily empty"
                ),
            ),
            evaluated=0, excluded_dark=0, window_start=window_start, gaps={},
        )

    evaluated = 0
    excluded_dark = 0
    gaps: dict[str, list[date]] = {}
    for r in rows:
        last_bar = r["last_bar"]
        first_bar = r["first_bar"]
        if last_bar is None or first_bar is None:
            continue
        # Liveness gate — fully-dark liquid name is a halt/delist
        # (owned by the delistings check), not a bars-ingest gap.
        if last_bar < live_floor:
            excluded_dark += 1
            continue

        evaluated += 1
        present: set = set(r["window_dates"] or ())
        # Expected = window sessions that fall inside this ticker's own
        # active range. Pre-IPO / post-last-bar sessions are never
        # demanded — that is the only legitimate exclusion.
        expected = {s for s in window_set if first_bar <= s <= last_bar}
        missing = sorted(expected - present)
        if missing:
            gaps[r["ticker"]] = missing

    return _Evaluation(
        sentinel=None,
        evaluated=evaluated,
        excluded_dark=excluded_dark,
        window_start=window_start,
        gaps=gaps,
    )


async def check_prices_daily_completeness(
    pool: asyncpg.Pool,
    source: Any = None,
) -> CheckResult:
    """Zero-tolerance: every liquid live name has a bar every session."""
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
            reason="missing_session",
            expected=f"a bar for every NYSE session in [{ev.window_start} … latest]",
            observed=f"{len(missing)} missing session(s): {shown}{more}",
        ))

    total_missing = len(failures)
    if total_missing == 0:
        logger.info(
            "tpcore.validation.completeness.ok",
            evaluated=ev.evaluated, excluded_dark=ev.excluded_dark,
        )
    else:
        logger.warning(
            "tpcore.validation.completeness.gap",
            offending_tickers=total_missing,
            evaluated=ev.evaluated, excluded_dark=ev.excluded_dark,
        )

    return CheckResult(
        name=CHECK_NAME,
        passed=total_missing == 0,
        total=max(ev.evaluated, 1),
        failed=total_missing,
        duration_ms=int((time.perf_counter() - started) * 1000),
        failures=failures[:MAX_REPORTED],
    )


async def compute_gap_repair_targets(
    pool: asyncpg.Pool,
) -> tuple[list[str], int]:
    """Targets for the bounded auto-heal: exactly the tickers the
    invariant flags and a ``lookback_days`` that brackets the oldest
    missing session.

    Returns ``([], 0)`` when there is nothing to repair OR when a
    structural sentinel (no_sessions / empty_liquid_universe) is active
    — those are NOT bars-backfill-fixable, so the caller must escalate
    rather than run a pointless re-pull. Because this shares
    :func:`_evaluate` with the check, the heal can never target a
    different set than the detector reports.
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
    "LIQUID_MIN_AVG_VOL_60D",
    "TRADEABLE_TIER_MAX",
    "WINDOW_SESSIONS",
    "check_prices_daily_completeness",
    "compute_gap_repair_targets",
]
