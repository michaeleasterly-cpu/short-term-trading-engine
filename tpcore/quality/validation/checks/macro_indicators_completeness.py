"""macro_indicators completeness — the ungameable per-cadence zero-gap invariant.

``macro_indicators_freshness`` answers "is the latest row of each
indicator newer than ``MAX_AGE_DAYS``?" — a recency probe with a
tolerance knob. It is structurally blind to *gaps inside* a series's
active history. The 2026-05-15 BAMLH0A0HYM2 (``hy_spread``) truncation
incident — FRED began serving a rolling 3-year window, dropping 20+
years of mid-range observations — passed freshness because the newest
date was still current; the gap was invisible.

This check closes that hole with a *physical-truth invariant* that has
no tolerance knob and no recency window:

    For every expected indicator, given its FRED-known publication
    cadence (DAILY/WEEKLY/MONTHLY), there must be a row for EVERY
    expected publication date in [first_observed_date,
    latest_observed_date]. One missing (indicator, date) → FAIL.

Cadence is a *physical truth* about each FRED series — direct
observation of the live DB (~9k rows for daily series over 36 years
aligns with ~252 business days × 36; ~1900 rows for ``initial_claims``
aligns with weekly Thursday-publication × 36; ~435 rows for monthly
series aligns with 36 × 12). The cadence map below is an explicit code
constant; cadence changes for a series are an explicit PR + test
update — they cannot accidentally bypass the invariant.

Why each scoping clause is a principled boundary and NOT a tolerance
knob that hides failures:

* **Within-active-range only** ``[first_observed, latest_observed]``
  per series — same principle as ``prices_daily_completeness`` not
  demanding pre-IPO bars; pre-history dates are not in scope. This is
  the only legitimate exclusion.
* **Per-cadence dispatch** — a single global cadence would either
  produce false-fails on monthly series or hide gaps on daily ones;
  matching the invariant to FRED's published cadence is the only
  correct partition.
* **Closed expected-indicator set** — identical to the freshness
  check's set; a new FRED series is an explicit code edit + test
  update.

The healer's symmetry ``compute_macro_repair_targets`` calls the
SAME ``_evaluate`` — detector and healer cannot disagree.

``cfnai_ma3`` (Chicago Fed National Activity Index, 3-month MA;
FRED series ``CFNAIMA3``, monthly publication) was added 2026-05-20
to unblock the Sentinel graduated Bear Score Lab candidate (TODO
§Deep-research) — the candidate's ``CFNAI ≤ -0.70`` band anchor
cannot fire without this series ingested.

``phci_<state>`` × 50 + the derived ``sos_state_diffusion`` were
added 2026-05-21 — same Sentinel candidate needs a ≥0.20 SOS
anchor. The 50 raw ``{XX}PHCI`` series feed the derived
``sos_state_diffusion`` (Crone/Clayton-Matthews 2005 sum-of-states
diffusion, 3-month span) via ``tpcore.fred.diffusion``. All 51
are MONTHLY.
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

CHECK_NAME = "macro_indicators_completeness"

# Closed set — identical to macro_indicators_freshness.EXPECTED_INDICATORS.
# A new FRED series MUST be added here AND given a cadence below;
# the consistency check (sibling freshness module) catches drift between
# the two sets.
EXPECTED_INDICATORS: tuple[str, ...] = (
    "vix",
    "yield_curve",
    "credit_spread",
    "hy_spread",
    "initial_claims",
    "industrial_production",
    "sahm_rule",
    "cfnai_ma3",
    # Chicago Fed National Financial Conditions Index — weekly composite of
    # credit + vol + liquidity. Added 2026-05-23 per financial-expert
    # recommendation. Used by sentinel + carver as a single regime gate.
    "nfci",
    # Secured Overnight Financing Rate (FRED 'SOFR') — daily since 2018-04-03.
    # USD short-rate signal + SOFR-OIS spread building block. Added 2026-05-24.
    "sofr",
    # Economic Policy Uncertainty (Baker-Bloom-Davis; FRED 'USEPUINDXD') —
    # daily since 1985. Catalyst event-window risk weighting. Added 2026-05-24.
    "epu_index",
    # ── Philadelphia Fed state coincident indices — 50 USPS states
    # (substrate for the derived sos_state_diffusion below).
    "phci_al", "phci_ak", "phci_az", "phci_ar", "phci_ca",
    "phci_co", "phci_ct", "phci_de", "phci_fl", "phci_ga",
    "phci_hi", "phci_id", "phci_il", "phci_in", "phci_ia",
    "phci_ks", "phci_ky", "phci_la", "phci_me", "phci_md",
    "phci_ma", "phci_mi", "phci_mn", "phci_ms", "phci_mo",
    "phci_mt", "phci_ne", "phci_nv", "phci_nh", "phci_nj",
    "phci_nm", "phci_ny", "phci_nc", "phci_nd", "phci_oh",
    "phci_ok", "phci_or", "phci_pa", "phci_ri", "phci_sc",
    "phci_sd", "phci_tn", "phci_tx", "phci_ut", "phci_vt",
    "phci_va", "phci_wa", "phci_wv", "phci_wi", "phci_wy",
    # Derived: Crone/Clayton-Matthews 2005 sum-of-states diffusion.
    "sos_state_diffusion",
)

# FRED publication cadence per series. DAILY = every NYSE session;
# WEEKLY = every Thursday (DOL initial-claims release day); MONTHLY =
# first calendar day of each month (sahm_rule / industrial_production
# anchor on month-start). These are physical truths about each
# FRED-published series, NOT tunable parameters.
CADENCE_DAILY: str = "daily"
CADENCE_WEEKLY: str = "weekly"
CADENCE_MONTHLY: str = "monthly"

INDICATOR_CADENCE: dict[str, str] = {
    "vix": CADENCE_DAILY,
    "yield_curve": CADENCE_DAILY,
    "credit_spread": CADENCE_DAILY,
    "hy_spread": CADENCE_DAILY,
    "initial_claims": CADENCE_WEEKLY,
    "industrial_production": CADENCE_MONTHLY,
    "sahm_rule": CADENCE_MONTHLY,
    "cfnai_ma3": CADENCE_MONTHLY,
    # NFCI publishes weekly (Wednesdays at 8:30am CT). Per FRED release calendar.
    "nfci": CADENCE_WEEKLY,
    # SOFR publishes daily (NY Fed, ~8am ET each business day).
    "sofr": CADENCE_DAILY,
    # EPU publishes daily (Baker-Bloom-Davis updates daily).
    "epu_index": CADENCE_DAILY,
    # 50 state PHCI series — each publishes monthly (Phila Fed).
    "phci_al": CADENCE_MONTHLY, "phci_ak": CADENCE_MONTHLY,
    "phci_az": CADENCE_MONTHLY, "phci_ar": CADENCE_MONTHLY,
    "phci_ca": CADENCE_MONTHLY, "phci_co": CADENCE_MONTHLY,
    "phci_ct": CADENCE_MONTHLY, "phci_de": CADENCE_MONTHLY,
    "phci_fl": CADENCE_MONTHLY, "phci_ga": CADENCE_MONTHLY,
    "phci_hi": CADENCE_MONTHLY, "phci_id": CADENCE_MONTHLY,
    "phci_il": CADENCE_MONTHLY, "phci_in": CADENCE_MONTHLY,
    "phci_ia": CADENCE_MONTHLY, "phci_ks": CADENCE_MONTHLY,
    "phci_ky": CADENCE_MONTHLY, "phci_la": CADENCE_MONTHLY,
    "phci_me": CADENCE_MONTHLY, "phci_md": CADENCE_MONTHLY,
    "phci_ma": CADENCE_MONTHLY, "phci_mi": CADENCE_MONTHLY,
    "phci_mn": CADENCE_MONTHLY, "phci_ms": CADENCE_MONTHLY,
    "phci_mo": CADENCE_MONTHLY, "phci_mt": CADENCE_MONTHLY,
    "phci_ne": CADENCE_MONTHLY, "phci_nv": CADENCE_MONTHLY,
    "phci_nh": CADENCE_MONTHLY, "phci_nj": CADENCE_MONTHLY,
    "phci_nm": CADENCE_MONTHLY, "phci_ny": CADENCE_MONTHLY,
    "phci_nc": CADENCE_MONTHLY, "phci_nd": CADENCE_MONTHLY,
    "phci_oh": CADENCE_MONTHLY, "phci_ok": CADENCE_MONTHLY,
    "phci_or": CADENCE_MONTHLY, "phci_pa": CADENCE_MONTHLY,
    "phci_ri": CADENCE_MONTHLY, "phci_sc": CADENCE_MONTHLY,
    "phci_sd": CADENCE_MONTHLY, "phci_tn": CADENCE_MONTHLY,
    "phci_tx": CADENCE_MONTHLY, "phci_ut": CADENCE_MONTHLY,
    "phci_vt": CADENCE_MONTHLY, "phci_va": CADENCE_MONTHLY,
    "phci_wa": CADENCE_MONTHLY, "phci_wv": CADENCE_MONTHLY,
    "phci_wi": CADENCE_MONTHLY, "phci_wy": CADENCE_MONTHLY,
    # Derived sum-of-states diffusion — also monthly (one row per
    # month where all 50 anchor states have an aligned PHCI(t) and
    # PHCI(t-3)).
    "sos_state_diffusion": CADENCE_MONTHLY,
}

# Weekly cadence anchor weekday per indicator. FRED's ``observation_date``
# (NOT the press-release date) is the field that lands in
# ``platform.macro_indicators.date``; the invariant must match that.
#
# * ``initial_claims`` (FRED series ``IC4WSA``, the 4-week MA) and
#   ``ICSA`` are both indexed by FRED on the **Saturday** ending the
#   reference week — verified against the FRED API metadata + the
#   live ``platform.macro_indicators`` rows (every existing
#   ``initial_claims`` row's date.weekday() == 5). The Thursday 8:30 ET
#   *release* time is when the press release lands; FRED still stores
#   the observation under the week-ending Saturday. The 2026-05-22
#   full-spectrum data-feed hardening audit caught this defect:
#   1042+ false-positive "missing Thursdays" on a check that expected
#   Thursday-anchored observations that FRED never publishes that way.
#
# Python weekday() — Monday=0, Thursday=3, Saturday=5. New WEEKLY
# indicators must add an entry here AND in INDICATOR_CADENCE; the
# completeness check raises ``cadence_unmapped`` if a WEEKLY indicator
# is missing here (the principled fail-loud invariant — no silent
# fall-back to a wrong default).
INDICATOR_WEEKLY_ANCHOR_WEEKDAY: dict[str, int] = {
    "initial_claims": 5,  # Saturday — FRED IC4WSA observation_date
}

# Backwards-compat: a WEEKLY indicator with no per-series anchor
# declared defaults to Thursday — preserves the pre-2026-05-22 behavior
# for any new WEEKLY series whose anchor weekday hasn't been declared,
# but the cadence-unmapped-style invariant below makes the missing entry
# loud enough to catch in CI on the very next run.
DEFAULT_WEEKLY_ANCHOR_WEEKDAY: int = 3  # Thursday

# Legacy public alias — preserved for downstream test imports that
# pinned the Thursday-anchor expectation directly (the old constant
# name documented the legacy assumption). Now an alias for the
# DEFAULT value above; per-indicator anchors are the SoT.
WEEKLY_ANCHOR_WEEKDAY: int = DEFAULT_WEEKLY_ANCHOR_WEEKDAY

# Failure list is capped for log size; CheckResult.failed always carries
# the TRUE total count so confidence reflects reality.
MAX_REPORTED = 25

# Buffer added to the computed repair lookback so the targeted re-pull
# comfortably brackets the oldest missing observation.
REPAIR_LOOKBACK_BUFFER_DAYS = 7


_INDICATOR_RANGE_SQL = """
    SELECT indicator,
           MIN(date) AS first_date,
           MAX(date) AS last_date,
           COUNT(*)  AS row_count
    FROM platform.macro_indicators
    WHERE indicator = ANY($1::text[])
    GROUP BY indicator
"""


_INDICATOR_DATES_SQL = """
    SELECT date
    FROM platform.macro_indicators
    WHERE indicator = $1 AND date BETWEEN $2 AND $3
"""


def _expected_dates_for_cadence(
    cadence: str,
    first: date,
    last: date,
    *,
    indicator: str | None = None,
) -> list[date]:
    """Pure helper — the canonical expected-publication-dates for a
    cadence between [first, last] inclusive.

    DAILY  → every NYSE session in range (XNYS via tpcore.calendar).
    WEEKLY → every observation-anchor-weekday in range. The anchor is
             per-indicator (``INDICATOR_WEEKLY_ANCHOR_WEEKDAY``);
             unmapped WEEKLY indicators fall back to
             ``DEFAULT_WEEKLY_ANCHOR_WEEKDAY`` (Thursday) to preserve
             the legacy expectation for any new series whose anchor is
             not yet declared. ``initial_claims`` (IC4WSA) anchors on
             Saturday per FRED's observation_date field.
    MONTHLY → first day of every month touched by [first, last].
    """
    if first > last:
        return []
    if cadence == CADENCE_DAILY:
        return cal.sessions_in_range(first, last)
    if cadence == CADENCE_WEEKLY:
        anchor = (
            INDICATOR_WEEKLY_ANCHOR_WEEKDAY.get(indicator, DEFAULT_WEEKLY_ANCHOR_WEEKDAY)
            if indicator is not None else DEFAULT_WEEKLY_ANCHOR_WEEKDAY
        )
        # First anchor-day on-or-after `first`.
        first_anchor = first + timedelta(
            days=(anchor - first.weekday()) % 7
        )
        out: list[date] = []
        d = first_anchor
        while d <= last:
            out.append(d)
            d += timedelta(days=7)
        return out
    if cadence == CADENCE_MONTHLY:
        # First day of each month in [first, last].
        out = []
        y, m = first.year, first.month
        while True:
            d = date(y, m, 1)
            if d > last:
                break
            if d >= first:
                out.append(d)
            m += 1
            if m == 13:
                m = 1
                y += 1
        return out
    # Unknown cadence is a coding error; fail loud rather than hide.
    raise ValueError(f"unknown cadence: {cadence!r}")


@dataclass(frozen=True)
class _Evaluation:
    """One completeness evaluation — shared by check + healer.

    Exactly one of ``sentinel`` (a structural failure that blocks
    verification entirely) or the gap fields is meaningful: if
    ``sentinel`` is set the others are zero/empty.
    """

    sentinel: FailureDetail | None
    evaluated: int
    missing_indicators: list[str]
    # indicator → sorted list of missing publication dates (within active range)
    gaps: dict[str, list[date]]


async def _evaluate(pool: asyncpg.Pool) -> _Evaluation:
    """Run the invariant once. Single source of truth for both
    ``check_macro_indicators_completeness`` (detection) and
    ``compute_macro_repair_targets`` (healing) — they cannot disagree."""
    async with pool.acquire() as conn:
        range_rows = await conn.fetch(
            _INDICATOR_RANGE_SQL, list(EXPECTED_INDICATORS)
        )

    by_indicator: dict[str, dict[str, Any]] = {
        r["indicator"]: {
            "first_date": r["first_date"],
            "last_date": r["last_date"],
            "row_count": int(r["row_count"] or 0),
        }
        for r in range_rows
    }

    # Detect missing-indicator-entirely (structural sentinel — re-pull
    # SHOULD fix this, but it's a different failure class than a gap,
    # and the check reports it separately so the operator sees the
    # category).
    missing_indicators: list[str] = [
        ind for ind in EXPECTED_INDICATORS
        if ind not in by_indicator or by_indicator[ind]["row_count"] == 0
    ]

    if not by_indicator:
        return _Evaluation(
            sentinel=FailureDetail(
                ticker="<macro_indicators>",
                reason="table_empty",
                expected=(
                    f"≥1 row for each of {len(EXPECTED_INDICATORS)} expected indicators"
                ),
                observed=(
                    "zero rows in platform.macro_indicators — initial "
                    "FRED ingest never ran or table truncated"
                ),
            ),
            evaluated=0,
            missing_indicators=list(EXPECTED_INDICATORS),
            gaps={},
        )

    gaps: dict[str, list[date]] = {}
    evaluated = 0
    for indicator, meta in by_indicator.items():
        first_d = meta["first_date"]
        last_d = meta["last_date"]
        if first_d is None or last_d is None:
            continue
        cadence = INDICATOR_CADENCE.get(indicator)
        if cadence is None:
            # New series in DB not yet declared in the cadence map —
            # fail loud (this is a deliberate consistency invariant,
            # not a tolerance knob).
            return _Evaluation(
                sentinel=FailureDetail(
                    ticker=indicator,
                    reason="cadence_unmapped",
                    expected=(
                        "every indicator in DB declared in "
                        "INDICATOR_CADENCE in macro_indicators_completeness.py"
                    ),
                    observed=(
                        f"indicator={indicator!r} present in DB but "
                        f"missing from INDICATOR_CADENCE — add it"
                    ),
                ),
                evaluated=evaluated,
                missing_indicators=missing_indicators,
                gaps={},
            )

        # Clamp first_d to the XNYS calendar's first known session.
        # exchange_calendars' default XNYS calendar starts 2006-05-22;
        # macro indicators with longer history (e.g. SOS PHCI series back
        # to 1979, vix back to 1990) would otherwise blow up
        # _expected_dates_for_cadence with "Parameter `start` cannot be
        # earlier than the first session of calendar 'XNYS'". Completeness
        # is only checked from the calendar's known range onward —
        # earlier rows are fine, they're just not subject to NYSE-session
        # gap detection. WEEKLY + MONTHLY cadences don't use the
        # calendar, so the clamp is safe for them too.
        clamped_first_d = max(first_d, cal.first_session())
        if clamped_first_d > last_d:
            evaluated += 1
            continue
        expected_dates = _expected_dates_for_cadence(
            cadence, clamped_first_d, last_d, indicator=indicator,
        )
        if not expected_dates:
            evaluated += 1
            continue

        async with pool.acquire() as conn:
            present_rows = await conn.fetch(
                _INDICATOR_DATES_SQL, indicator, first_d, last_d
            )
        present: set[date] = {r["date"] for r in present_rows}

        missing = sorted(set(expected_dates) - present)
        if missing:
            gaps[indicator] = missing
        evaluated += 1

    return _Evaluation(
        sentinel=None,
        evaluated=evaluated,
        missing_indicators=missing_indicators,
        gaps=gaps,
    )


async def check_macro_indicators_completeness(
    pool: asyncpg.Pool,
    source: Any = None,
) -> CheckResult:
    """Zero-tolerance: every expected publication date present per
    series within its observed active range."""
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

    # Missing-indicator-entirely failures first.
    for ind in ev.missing_indicators:
        failures.append(FailureDetail(
            ticker=ind,
            reason="indicator_missing",
            expected=f"≥1 row for indicator={ind!r}",
            observed=(
                "zero rows present — initial ingest never ran for "
                "this series, or all rows truncated"
            ),
        ))

    # Per-indicator gap failures.
    for indicator, missing in sorted(ev.gaps.items()):
        cadence = INDICATOR_CADENCE.get(indicator, "?")
        shown = ", ".join(d.isoformat() for d in missing[:8])
        more = "" if len(missing) <= 8 else f" (+{len(missing) - 8} more)"
        failures.append(FailureDetail(
            ticker=indicator,
            reason="missing_publication",
            expected=(
                f"a row for every {cadence} publication date in "
                f"[{missing[0].isoformat() if missing else '?'} … latest]"
            ),
            observed=f"{len(missing)} missing date(s): {shown}{more}",
        ))

    total_failed = len(failures)
    if total_failed == 0:
        logger.info(
            "tpcore.validation.macro_completeness.ok",
            evaluated=ev.evaluated,
        )
    else:
        logger.warning(
            "tpcore.validation.macro_completeness.gap",
            offending_indicators=total_failed,
            missing_indicators=len(ev.missing_indicators),
            gap_indicators=len(ev.gaps),
            evaluated=ev.evaluated,
        )

    return CheckResult(
        name=CHECK_NAME,
        passed=total_failed == 0,
        total=max(ev.evaluated + len(ev.missing_indicators), 1),
        failed=total_failed,
        duration_ms=int((time.perf_counter() - started) * 1000),
        failures=failures[:MAX_REPORTED],
    )


async def compute_macro_repair_targets(
    pool: asyncpg.Pool,
) -> tuple[list[str], int]:
    """Targets for the bounded auto-heal: indicators with gaps + a
    ``lookback_days`` that brackets the oldest missing date.

    Returns ``([], 0)`` when there is nothing to repair OR when a
    structural sentinel is active (table empty, cadence unmapped) —
    those are NOT bars-backfill-fixable, so the caller must escalate
    rather than run a pointless re-pull. Shares :func:`_evaluate` with
    the check; heal can never target a different set than the
    detector reports.

    Note: the canonical ``macro_indicators`` stage re-pulls all 7
    series in one shot (there is no per-series scoping at the stage
    level — the universe IS the 7 series). The returned ``indicators``
    list is informational for the heal log; the ``lookback_days``
    controls how far back the stage backfills.
    """
    ev = await _evaluate(pool)
    if ev.sentinel is not None:
        return [], 0
    indicators_with_gaps = sorted(ev.gaps)
    if not indicators_with_gaps and not ev.missing_indicators:
        return [], 0

    targets = sorted(set(indicators_with_gaps) | set(ev.missing_indicators))

    today = datetime.now(UTC).date()
    oldest_missing: date | None = None
    for missing in ev.gaps.values():
        if not missing:
            continue
        d = missing[0]
        if oldest_missing is None or d < oldest_missing:
            oldest_missing = d
    if oldest_missing is None:
        # missing-indicator-entirely case: re-pull the full history
        # (the canonical stage's default lookback is sufficient — pass 0
        # so the stage uses its built-in default).
        return targets, 0
    lookback_days = (today - oldest_missing).days + REPAIR_LOOKBACK_BUFFER_DAYS
    return targets, lookback_days


__all__ = [
    "CADENCE_DAILY",
    "CADENCE_MONTHLY",
    "CADENCE_WEEKLY",
    "CHECK_NAME",
    "DEFAULT_WEEKLY_ANCHOR_WEEKDAY",
    "EXPECTED_INDICATORS",
    "INDICATOR_CADENCE",
    "INDICATOR_WEEKLY_ANCHOR_WEEKDAY",
    "WEEKLY_ANCHOR_WEEKDAY",
    "check_macro_indicators_completeness",
    "compute_macro_repair_targets",
]
