"""fundamentals_quarterly completeness — cadence-routed periodic-filing invariant.

**P1 rewrite (2026-05-30)** — routing by ``ticker_classifications.sec_document_type_primary``
replaces the prior ``asset_class = 'stock'`` predicate. The old quarterly-cadence
gate fired false positives on every foreign private issuer (20-F / 40-F filers)
that files annually rather than quarterly. Today's 173-ticker failing set is
~40% 20-F filers (AER, ARCO, ARQQ, AU, BIP, BIPC, BWMX, CAMT, …) that this
rewrite correctly reclassifies as PASS at annual cadence.

The dispositive routing signal is the SEC-derived ``sec_document_type_primary``
column populated by the P0 ``backfill_sec_metadata`` stage (commit 2eca8c7):

  ============= ========================== =================
  Primary form  Cadence                    Max consecutive
                                           filing-gap days
  ============= ========================== =================
  10-Q          quarterly                  100  (= 92 + 8 slack)
  10-K          annual                     450
  20-F          annual                     450
  40-F          annual                     450
  ============= ========================== =================

The 450-day annual cap is calibrated for foreign-private-issuer 20-F
deadlines (4 months after fiscal year end) and short-late filers: a
true year-skip is ~730 days (two FY ends), so 450 leaves ~85 days of
late-filing slack without false-firing on a legitimately-late filer.

# Five-state semantics

Encoded via per-ticker exclusion buckets (precedent: existing ``excluded_dark``).
Each ticker is in exactly one state per evaluation:

  PASS                  — filings present at expected cadence; in evaluated_routed
                          denominator; contributes nothing to ``failures``.
  FAIL                  — cadence gap detected; ``FailureDetail(reason=
                          "missing_period_<form>", …)``.
  METADATA_REQUIRED     — ``sec_document_type_primary IS NULL``; CANNOT be routed.
                          Excluded from denominator; counted in
                          ``excluded_metadata_required``. NEVER counts as a
                          per-ticker FAIL — the operator-actionable signal lives
                          at the suite level via a metadata-coverage sentinel
                          (see below).
  CONFIRMED_DATA_GAP    — ticker has < 2 filings in active range AND is past the
                          new-listing grace window (issuer-age-aware threshold,
                          see ``_NEW_LISTING_GRACE_DAYS``). Excluded from
                          denominator; counted in ``excluded_confirmed_data_gap``.
                          NOT a defect.
  BLOCKED_VENDOR_ACCESS — reserved for P2 (vendor-error surface; not detectable
                          from this DB-only check).

# Metadata-coverage structural sentinel

If ``excluded_metadata_required / (evaluated_routed + excluded_metadata_required)
> METADATA_COVERAGE_FAIL_THRESHOLD`` (default 0.25 = 25%), the check emits a
synthetic ``FailureDetail(ticker="<metadata_coverage>", reason=
"metadata_coverage_insufficient", …)`` so the suite hard-stops until the
``backfill_sec_metadata`` stage extends coverage. Without this sentinel a P0
backfill regression silently passes the check.

At commit 2eca8c7 metadata coverage was 362 / 13,840 = **2.6%** — far below
the 25% threshold. **DATA_OPERATIONS_COMPLETE remains blocked post-P1 until
backfill coverage reaches > 75% of the routed-eligible universe.** This is the
correct outcome, not a regression.

# Liveness windows are cadence-routed

The pre-P1 ``LIVE_WITHIN_DAYS = 120`` silently darkened every annual filer
(a 20-F filer last-filed > 4 months ago looks "dark" by quarterly standards).
Liveness is now per-cadence:

  ``LIVE_WITHIN_DAYS_QUARTERLY = 120``  (unchanged for 10-Q)
  ``LIVE_WITHIN_DAYS_ANNUAL    = 540``  (18 months — covers worst-case
                                           missed-by-one-year before darkening)

# Detector / healer parity

``compute_fundamentals_repair_targets`` continues to share ``_evaluate`` with
the check (existing invariant). The healer ONLY targets tickers in the
``gaps`` set — never METADATA_REQUIRED / CONFIRMED_DATA_GAP / synthetic
``<metadata_coverage>`` tickers (those aren't fundamentals-refresh-fixable).

# CheckResult shape preserved

The frozen ``CheckResult`` model is unchanged. Diagnostic counters
(``evaluated_routed``, ``excluded_dark``, ``excluded_metadata_required``,
``excluded_confirmed_data_gap``, ``by_form``) are logged via structlog at
completion, not serialized into ``CheckResult`` (which is ``frozen=True
extra=forbid``).
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

# Universe boundary — identical to prices_daily_completeness on tier;
# asset_class predicate is REPLACED by sec_document_type_primary routing
# (P1 rewrite). Foreign filers (asset_class often 'adr' or 'stock' with
# foreign country) are now correctly judged by their actual cadence.
TRADEABLE_TIER_MAX = 2

# Cadence forms. The check ROUTES on these; any other form value falls
# into the OTHER_FORM exclusion bucket (e.g. ``N-1A`` for closed-end
# funds — not a periodic operating-company filing).
#
# Note: amendment variants (``10-Q/A``, ``10-K/A``, ``20-F/A``, ``40-F/A``)
# are NOT listed here by design — the P0 ``extract_filing_metadata``
# primitive collapses ``/A`` amendments to their base form for primary
# classification (see ``tpcore/sec/companyfacts_adapter.py`` lines
# 351-357). So a 10-Q/A-only filer's primary form lands as ``10-Q``;
# this routing set is correct.
_QUARTERLY_FORMS: frozenset[str] = frozenset({"10-Q"})
_ANNUAL_FORMS: frozenset[str] = frozenset({"10-K", "20-F", "40-F"})
_ROUTED_FORMS: frozenset[str] = _QUARTERLY_FORMS | _ANNUAL_FORMS

# Quarterly cadence — Q4 is 92 days + 8-day late-filing slack.
MAX_QUARTERLY_GAP_DAYS = 100

# Annual cadence — 365 + 4-month 20-F deadline + ~30-day late-filing
# slack. A true year-skip is ~730 days (two consecutive FY ends);
# 450 leaves headroom without false-firing legitimately-late 20-F.
MAX_ANNUAL_GAP_DAYS = 450

# Per-cadence liveness gates. The pre-P1 single 120-day window silently
# excluded annual filers (a 20-F filer just past their 4-month deadline
# looks "dark" by quarterly standards). Each form gets its own window.
LIVE_WITHIN_DAYS_QUARTERLY = 120
LIVE_WITHIN_DAYS_ANNUAL = 540

# CONFIRMED_DATA_GAP threshold: a ticker with < 2 filings is either
# a brand-new listing (PASS — grace window) or a true data hole. The
# discriminator is issuer-age vs cadence window.
_NEW_LISTING_GRACE_QUARTERLY_DAYS = MAX_QUARTERLY_GAP_DAYS * 2  # ~200d
_NEW_LISTING_GRACE_ANNUAL_DAYS = MAX_ANNUAL_GAP_DAYS  # ~450d

# Metadata-coverage structural sentinel — fires when routing fails on
# too high a fraction of the active universe. 25% is the P1 floor; at
# commit 2eca8c7 the live coverage was 2.6% (sentinel fires correctly).
METADATA_COVERAGE_FAIL_THRESHOLD = 0.25

# Failure list cap for log size; CheckResult.failed always carries the
# TRUE total count so confidence reflects reality.
MAX_REPORTED = 25

# Buffer added to the computed repair lookback so the targeted re-pull
# comfortably brackets the oldest missing period (filing-date math has
# month-end variance + the new annual cadence widens range).
REPAIR_LOOKBACK_BUFFER_DAYS = 14

# Synthetic ticker IDs used for whole-check sentinels (not real tickers,
# parallel to the existing ``<universe>`` empty-universe sentinel).
_METADATA_COVERAGE_SENTINEL_TICKER = "<metadata_coverage>"
_UNIVERSE_SENTINEL_TICKER = "<universe>"


_FILING_DATES_SQL = """
    WITH liquid AS (
        SELECT lt.ticker, tc.sec_document_type_primary
        FROM platform.liquidity_tiers lt
        JOIN platform.ticker_classifications tc ON tc.ticker = lt.ticker
        WHERE lt.tier <= $1
    )
    SELECT fq.ticker, fq.period_end_date, liquid.sec_document_type_primary
    FROM platform.fundamentals_quarterly fq
    JOIN liquid USING (ticker)
    WHERE fq.period_end_date IS NOT NULL
    ORDER BY fq.ticker, fq.period_end_date
"""


@dataclass(frozen=True)
class _Evaluation:
    """One evaluation — shared by check + healer.

    Exactly one of ``sentinel`` or the gap / counter fields is the
    meaningful payload: a structural sentinel short-circuits the
    invariant and the counters are zero/empty.
    """

    sentinel: FailureDetail | None
    evaluated_routed: int
    excluded_dark: int
    excluded_metadata_required: int
    excluded_confirmed_data_gap: int
    excluded_other_form: int
    by_form: dict[str, int]
    # ticker → (sorted list of inferred missing period_end_dates, form)
    gaps: dict[str, tuple[list[date], str]]
    # Set when the metadata-coverage sentinel must additionally fire.
    metadata_coverage_low: bool = False
    metadata_coverage_ratio: float = 0.0


def _infer_missing_period_ends(
    earlier: date, later: date, *, max_gap_days: int, period_days: int,
) -> list[date]:
    """Given two consecutive present filings ~Nx periods apart, return
    the inferred missing period-ends between them.

    Approximates by placing missing period-ends evenly between the two
    anchors. The healer uses ONLY the earliest missing date (to set
    ``lookback_days``); the check uses the count for logging. Exact
    calendar-period-snapping isn't needed — gaps are the signal, the
    inferred dates are advisory.

    ``period_days`` is the per-cadence typical period length (92 for
    quarterly, 365 for annual); used to estimate the count of missing
    periods.
    """
    gap_days = (later - earlier).days
    if gap_days <= max_gap_days:
        return []
    n_missing = max(1, round(gap_days / period_days) - 1)
    out: list[date] = []
    for i in range(1, n_missing + 1):
        offset = int(round(gap_days * i / (n_missing + 1)))
        out.append(earlier + timedelta(days=offset))
    return out


def _cadence_for(primary_form: str | None) -> tuple[str, int, int, int] | None:
    """Return (cadence_name, max_gap_days, live_within_days, period_days)
    for the given primary form, or None if not routable.

    The 4th component (period_days) feeds the missing-period inference
    heuristic — 92 for quarterly, 365 for annual.
    """
    if primary_form in _QUARTERLY_FORMS:
        return ("quarterly", MAX_QUARTERLY_GAP_DAYS,
                LIVE_WITHIN_DAYS_QUARTERLY, 92)
    if primary_form in _ANNUAL_FORMS:
        return ("annual", MAX_ANNUAL_GAP_DAYS,
                LIVE_WITHIN_DAYS_ANNUAL, 365)
    return None


async def _evaluate(pool: asyncpg.Pool) -> _Evaluation:
    """Run the invariant once. Single source of truth for both
    ``check_fundamentals_quarterly_completeness`` (detection) and
    ``compute_fundamentals_repair_targets`` (healing) — they cannot
    disagree because they are the same code."""
    today = datetime.now(UTC).date()

    async with pool.acquire() as conn:
        rows = await conn.fetch(_FILING_DATES_SQL, TRADEABLE_TIER_MAX)

    if not rows:
        return _Evaluation(
            sentinel=FailureDetail(
                ticker=_UNIVERSE_SENTINEL_TICKER,
                reason="empty_liquid_universe",
                expected=(
                    f"tier≤{TRADEABLE_TIER_MAX} ticker with fundamentals "
                    f"filings to exist"
                ),
                observed=(
                    "zero T1/T2 filings resolved — "
                    "fundamentals_quarterly empty or liquidity_tiers/"
                    "ticker_classifications stale"
                ),
            ),
            evaluated_routed=0, excluded_dark=0,
            excluded_metadata_required=0,
            excluded_confirmed_data_gap=0,
            excluded_other_form=0,
            by_form={}, gaps={},
        )

    # Group filings by ticker; capture each ticker's primary form (a
    # ticker can appear in multiple rows but the form is invariant per
    # row since it comes from the joined classification row).
    per_ticker: dict[str, list[date]] = {}
    primary_by_ticker: dict[str, str | None] = {}
    for r in rows:
        per_ticker.setdefault(r["ticker"], []).append(r["period_end_date"])
        primary_by_ticker[r["ticker"]] = r["sec_document_type_primary"]

    evaluated_routed = 0
    excluded_dark = 0
    excluded_metadata_required = 0
    excluded_confirmed_data_gap = 0
    excluded_other_form = 0
    by_form: dict[str, int] = {}
    gaps: dict[str, tuple[list[date], str]] = {}

    for ticker, period_ends in per_ticker.items():
        if not period_ends:
            continue
        primary = primary_by_ticker.get(ticker)
        cadence = _cadence_for(primary)
        if cadence is None:
            # Two sub-cases: NULL primary form → METADATA_REQUIRED.
            # Any other non-routed form (e.g. ``N-1A`` for closed-end
            # funds) → OTHER_FORM. Both EXCLUDED from the denominator.
            if primary is None:
                excluded_metadata_required += 1
            else:
                excluded_other_form += 1
            continue

        cadence_name, max_gap, live_within, period_days = cadence
        last_filed = period_ends[-1]
        first_filed = period_ends[0]

        # Per-cadence liveness gate. A 20-F filer just past their
        # 4-month deadline is NOT dark; an analogous 10-Q filer past
        # 120 days IS dark.
        if (today - last_filed).days > live_within:
            excluded_dark += 1
            continue

        # CONFIRMED_DATA_GAP: < 2 filings + issuer-age past grace.
        # Brand-new listings (first filing within the cadence grace
        # window) PASS silently — there's only one filing because the
        # company just started reporting. A single ancient filing past
        # the grace window IS a data hole.
        grace_days = (
            _NEW_LISTING_GRACE_ANNUAL_DAYS
            if cadence_name == "annual"
            else _NEW_LISTING_GRACE_QUARTERLY_DAYS
        )
        if len(period_ends) < 2:
            if (today - first_filed).days > grace_days:
                excluded_confirmed_data_gap += 1
            else:
                # New-listing grace: count as evaluated_routed (we did
                # judge them) but no gap to detect.
                evaluated_routed += 1
                by_form[primary] = by_form.get(primary, 0) + 1
            continue

        # Routed-eligible with ≥ 2 filings — full gap evaluation.
        evaluated_routed += 1
        by_form[primary] = by_form.get(primary, 0) + 1
        ticker_gaps: list[date] = []
        for i in range(1, len(period_ends)):
            earlier = period_ends[i - 1]
            later = period_ends[i]
            inferred = _infer_missing_period_ends(
                earlier, later,
                max_gap_days=max_gap, period_days=period_days,
            )
            ticker_gaps.extend(inferred)
        if ticker_gaps:
            gaps[ticker] = (sorted(ticker_gaps), primary)

    # Metadata-coverage structural sentinel — Q4 expert finding. If
    # too much of the active universe is METADATA_REQUIRED, the
    # routed-PASS verdict is structurally insufficient evidence.
    metadata_denom = evaluated_routed + excluded_metadata_required
    coverage_ratio = (
        excluded_metadata_required / metadata_denom
        if metadata_denom > 0 else 0.0
    )
    metadata_coverage_low = (
        coverage_ratio > METADATA_COVERAGE_FAIL_THRESHOLD
    )

    return _Evaluation(
        sentinel=None,
        evaluated_routed=evaluated_routed,
        excluded_dark=excluded_dark,
        excluded_metadata_required=excluded_metadata_required,
        excluded_confirmed_data_gap=excluded_confirmed_data_gap,
        excluded_other_form=excluded_other_form,
        by_form=by_form,
        gaps=gaps,
        metadata_coverage_low=metadata_coverage_low,
        metadata_coverage_ratio=coverage_ratio,
    )


async def check_fundamentals_quarterly_completeness(
    pool: asyncpg.Pool,
    source: Any = None,
) -> CheckResult:
    """Cadence-routed periodic-filing completeness check.

    PASS iff:
      * every routed-eligible ticker (10-Q / 10-K / 20-F / 40-F primary)
        has filings at expected cadence, AND
      * metadata coverage of the active universe is ≥
        ``METADATA_COVERAGE_FAIL_THRESHOLD`` (the structural sentinel).

    Routed-eligible: ``last_filed`` within per-cadence liveness window
    AND ``sec_document_type_primary`` is one of {10-Q, 10-K, 20-F, 40-F}.
    """
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

    # Metadata-coverage structural sentinel — prepended so the
    # operator-visible signal is preserved even when MAX_REPORTED
    # truncates per-ticker failures. CheckResult.failed counts the
    # TRUE total (sentinel + cadence misses) regardless of slice.
    if ev.metadata_coverage_low:
        metadata_denom = ev.evaluated_routed + ev.excluded_metadata_required
        pct = int(round(ev.metadata_coverage_ratio * 100))
        threshold_pct = int(round(METADATA_COVERAGE_FAIL_THRESHOLD * 100))
        failures.append(FailureDetail(
            ticker=_METADATA_COVERAGE_SENTINEL_TICKER,
            reason="metadata_coverage_insufficient",
            expected=(
                f"≤ {threshold_pct}% of active universe with NULL "
                f"sec_document_type_primary (routing required)"
            ),
            observed=(
                f"{ev.excluded_metadata_required} of {metadata_denom} active "
                f"({pct}%) lack sec_document_type_primary — extend the "
                f"backfill_sec_metadata stage to clear"
            ),
        ))

    # Per-ticker cadence failures.
    for ticker, (missing, form) in sorted(ev.gaps.items()):
        cadence_name, max_gap, _live, _period = _cadence_for(form) or (
            "unknown", 0, 0, 0
        )
        shown = ", ".join(d.isoformat() for d in missing[:8])
        more = "" if len(missing) <= 8 else f" (+{len(missing) - 8} more)"
        failures.append(FailureDetail(
            ticker=ticker,
            reason=f"missing_period_{form}",
            expected=(
                f"no consecutive filing gap > {max_gap} days "
                f"(cadence={cadence_name}, form={form})"
            ),
            observed=(
                f"{len(missing)} inferred missing period(s) at: {shown}{more}"
            ),
        ))

    total_failed = len(failures)
    passed = total_failed == 0

    if passed:
        logger.info(
            "tpcore.validation.fundamentals_completeness.ok",
            evaluated_routed=ev.evaluated_routed,
            excluded_dark=ev.excluded_dark,
            excluded_metadata_required=ev.excluded_metadata_required,
            excluded_confirmed_data_gap=ev.excluded_confirmed_data_gap,
            excluded_other_form=ev.excluded_other_form,
            by_form=ev.by_form,
            metadata_coverage_ratio=round(ev.metadata_coverage_ratio, 4),
        )
    else:
        logger.warning(
            "tpcore.validation.fundamentals_completeness.gap",
            offending_tickers=total_failed,
            evaluated_routed=ev.evaluated_routed,
            excluded_dark=ev.excluded_dark,
            excluded_metadata_required=ev.excluded_metadata_required,
            excluded_confirmed_data_gap=ev.excluded_confirmed_data_gap,
            excluded_other_form=ev.excluded_other_form,
            by_form=ev.by_form,
            metadata_coverage_low=ev.metadata_coverage_low,
            metadata_coverage_ratio=round(ev.metadata_coverage_ratio, 4),
        )

    return CheckResult(
        name=CHECK_NAME,
        passed=passed,
        total=max(ev.evaluated_routed, 1),
        failed=total_failed,
        duration_ms=int((time.perf_counter() - started) * 1000),
        failures=failures[:MAX_REPORTED],
    )


async def compute_fundamentals_repair_targets(
    pool: asyncpg.Pool,
) -> tuple[list[str], int]:
    """Targets for the bounded auto-heal: tickers with at least one
    inferred missing period in their routed cadence + a ``lookback_days``
    that brackets the oldest missing period.

    Returns ``([], 0)`` when nothing to repair OR when a structural
    sentinel is active — those are NOT bars-backfill-fixable, so the
    caller must escalate rather than run a pointless re-pull. Shares
    :func:`_evaluate` with the check; heal can never target a different
    set than the detector reports.

    METADATA_REQUIRED tickers are NEVER repair targets (operator action
    via ``backfill_sec_metadata`` is the right fix; ``fundamentals_refresh``
    would burn the SEC rate budget for no gain). The metadata-coverage
    synthetic ticker is likewise never returned.
    """
    ev = await _evaluate(pool)
    if ev.sentinel is not None or not ev.gaps:
        return [], 0
    tickers = sorted(ev.gaps)
    oldest_missing = min(
        d for missing, _form in ev.gaps.values() for d in missing
    )
    today = datetime.now(UTC).date()
    lookback_days = (today - oldest_missing).days + REPAIR_LOOKBACK_BUFFER_DAYS
    return tickers, lookback_days


__all__ = [
    "CHECK_NAME",
    "LIVE_WITHIN_DAYS_ANNUAL",
    "LIVE_WITHIN_DAYS_QUARTERLY",
    "MAX_ANNUAL_GAP_DAYS",
    "MAX_QUARTERLY_GAP_DAYS",
    "METADATA_COVERAGE_FAIL_THRESHOLD",
    "TRADEABLE_TIER_MAX",
    "check_fundamentals_quarterly_completeness",
    "compute_fundamentals_repair_targets",
]
