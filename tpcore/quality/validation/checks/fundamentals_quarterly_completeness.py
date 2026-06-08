"""fundamentals_quarterly completeness — authoritative SEC reportDate gate.

**P3 rewrite (2026-06-07)** — the gap is now an **authoritative
set-difference** against the SEC periodic-filings substrate
(``platform.sec_periodic_filings``), NOT an even-spacing interpolation
heuristic. The shared store ``tpcore.quality.sec_periodic_filings_store``
is the SINGLE source of the routed-form SoT + the gap computation; the
check and the SEC-fundamentals healer both delegate to it, so
detector/healer parity is REAL (one helper, not two month-stepping
copies that could drift).

  expected ⇐ DISTINCT ``report_date`` SEC actually filed for the issuer
             (filtered to the cadence's routed forms — 10-Q for
             quarterly; 10-K/20-F/40-F for annual; ``/A`` amendments
             collapse to base and are set-difference neutral).
  have     ⇐ DISTINCT ``period_end_date`` present in
             ``fundamentals_quarterly`` for the issuer.
  missing  ⇐ ``sorted(expected - have)`` — the GENUINE gap; every named
             date is a reportDate the SEC filed but our fundamentals
             substrate lacks. No interpolation, no inference: a 53-week
             fiscal year, a 10-K-replaces-Q4 annual filer, and a
             restatement amendment all fall out correctly because we only
             ever demand the reportDates SEC literally filed.

The dispositive ROUTING signal is still the SEC-derived
``sec_document_type_primary`` column (P0 ``backfill_sec_metadata`` stage):
base-form 10-Q ⇒ quarterly cadence; 10-K / 20-F / 40-F ⇒ annual cadence.
The routed-form sets themselves are imported from the shared store
(``ROUTED_QUARTERLY_FORMS`` / ``ROUTED_ANNUAL_FORMS`` via ``base_form``)
— this module does NOT keep a second copy.

# Anchored: the false-green discriminator (the core safety property)

``FilingGapResult.anchored`` (from the shared store) is dispositive:

  * ``anchored=True, missing_periods=()``  ⇒ SEC evidence exists and
    fundamentals has every filed reportDate ⇒ ticker PASSES.
  * ``anchored=True, missing_periods=[…]`` ⇒ SEC says these reportDates
    should exist but fundamentals lacks them ⇒ ticker FAILS, the missing
    reportDates are NAMED (after the dual-source evidence-join routes any
    confirmed-empty periods to ``excluded_confirmed_data_gap``).
  * ``anchored=False`` (ZERO ``sec_periodic_filings`` rows for the issuer)
    ⇒ there is NO SEC periodic evidence we can verify against, so the
    issuer MUST NOT silently PASS:
      - **CIK-less** tier≤2 names (no SEC obligation we can verify) route
        to ``excluded_confirmed_data_gap`` (excluded-WITH-evidence), NOT
        PASS and NOT a fabricated gap.
      - **CIK-backed** issuers that are ``anchored=False`` mean the
        periodic-filings backfill hasn't populated them yet — that must
        SURFACE as ``excluded_metadata_required`` (and feed the
        metadata-coverage sentinel below), NEVER a silent pass.

Collapsing ``anchored=False`` into a PASS would re-introduce the exact
false-green this substrate exists to prevent.

# Five-state semantics

Encoded via per-ticker exclusion buckets (precedent: existing ``excluded_dark``).
Each ticker is in exactly one state per evaluation:

  PASS                  — filings present at expected cadence; in evaluated_routed
                          denominator; contributes nothing to ``failures``.
  FAIL                  — cadence gap detected; ``FailureDetail(reason=
                          "missing_period_<form>", …)``.
  METADATA_REQUIRED     — ``sec_document_type_primary IS NULL`` (cannot route)
                          OR a CIK-backed issuer that is ``anchored=False`` (the
                          periodic-filings backfill has not populated it yet).
                          Excluded from denominator; counted in
                          ``excluded_metadata_required``. NEVER counts as a
                          per-ticker FAIL — the operator-actionable signal lives
                          at the suite level via a metadata-coverage sentinel
                          (see below).
  CONFIRMED_DATA_GAP    — a CIK-LESS issuer that is ``anchored=False`` (no SEC
                          periodic obligation we can verify) OR a period the
                          dual-source evidence-join confirms empty. Excluded
                          from denominator; counted in
                          ``excluded_confirmed_data_gap``. NOT a defect.
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

# Detector / healer parity (now REAL — a single shared helper)

``compute_fundamentals_repair_targets`` + ``compute_fundamentals_gap_periods``
share ``_evaluate`` with the check (existing invariant), AND ``_evaluate``
itself now delegates the gap math to
``tpcore.quality.sec_periodic_filings_store.compute_filing_gaps``. The
SEC-fundamentals healer (``tpcore.ingestion.handlers
.handle_sec_fundamentals_fallback``) delegates to the SAME store helper
(``compute_filing_gap``). There is no longer a second month-stepping copy
of the gap math anywhere: detector and healer cannot disagree because
they compute the gap with one function over one substrate.

The healer ONLY targets tickers in the ``gaps`` set — never
METADATA_REQUIRED / CONFIRMED_DATA_GAP / synthetic ``<metadata_coverage>``
tickers (those aren't fundamentals-refresh-fixable).

# CheckResult shape preserved

The frozen ``CheckResult`` model is unchanged. Diagnostic counters
(``evaluated_routed``, ``excluded_dark``, ``excluded_metadata_required``,
``excluded_confirmed_data_gap``, ``by_form``) are logged via structlog at
completion, not serialized into ``CheckResult`` (which is ``frozen=True
extra=forbid``).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.quality.confirmed_data_gap_store import (
    EVIDENCE_JOIN_SQL as _CONFIRMED_DATA_GAP_EVIDENCE_JOIN_SQL,
)
from tpcore.quality.sec_periodic_filings_store import (
    ROUTED_ANNUAL_FORMS as _STORE_ROUTED_ANNUAL_FORMS,
)
from tpcore.quality.sec_periodic_filings_store import (
    ROUTED_QUARTERLY_FORMS as _STORE_ROUTED_QUARTERLY_FORMS,
)
from tpcore.quality.sec_periodic_filings_store import (
    base_form,
    compute_filing_gaps,
)
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

# ── ≥2016 completeness horizon (2026-06-07, operator decision) ─────────
# The short-term DAILY engines (reversion / vector / momentum / sentinel /
# canary / catalyst) do not read pre-2016 fundamentals; 87% of the genuine
# gaps (5,959 of 6,819 reportDates at the time of this decision) are
# pre-2016 SEC history FMP never backfilled. The completeness invariant is
# scoped to an engine-relevant horizon: an expected SEC ``report_date``
# BEFORE this date is EXCLUDED-WITH-EVIDENCE (the SEC reportDate
# demonstrably exists in ``sec_periodic_filings`` and is excluded by
# documented horizon policy), counted in the surfaced ``excluded_pre_horizon``
# bucket and logged — NOT silently dropped, NOT a per-ticker FAIL. A
# reportDate ON/AFTER the horizon that is missing STILL FAILS (no masking
# of recent gaps).
#
# The horizon is the POLICY (it lives here, in the check); the shared store
# stays policy-free and merely applies the cut-off passed to
# ``compute_filing_gaps(horizon=...)``.
FUNDAMENTALS_COMPLETENESS_HORIZON_DEFAULT = date(2016, 1, 1)

# Env override (codebase config pattern: ``os.getenv``). ``STE_FUNDAMENTALS_HORIZON``
# (ISO ``YYYY-MM-DD``) overrides the default at evaluation time; an unset /
# malformed value falls back to the default (fail-loud via log, fail-safe to
# the documented horizon). Resolved per-evaluation (not import-time) so tests
# + ops can set it without a module reload.
_FUNDAMENTALS_HORIZON_ENV = "STE_FUNDAMENTALS_HORIZON"


def _resolve_horizon() -> date:
    """Return the active ≥2016 completeness horizon.

    Reads ``STE_FUNDAMENTALS_HORIZON`` (ISO date) if set + parseable; else
    the documented :data:`FUNDAMENTALS_COMPLETENESS_HORIZON_DEFAULT`. A
    malformed override is logged and ignored (fail-safe to the default — a
    bad env var must never silently widen OR narrow the safety gate).

    WIDEN-ONLY CLAMP (safety review #3): an override may only move the
    horizon EARLIER than the default (which WIDENS the failing set — strictly
    safe: more pre-horizon SEC depth is brought back in-scope). An override
    LATER than the default would NARROW the gate (shrink the failing set,
    mask recent-ish gaps) and is REJECTED — ignored with a ``logger.warning``
    and the default used instead. Any active horizon that differs from the
    default is logged at WARNING so a shift is always loud.
    """
    raw = os.getenv(_FUNDAMENTALS_HORIZON_ENV)
    if not raw:
        return FUNDAMENTALS_COMPLETENESS_HORIZON_DEFAULT
    try:
        parsed = date.fromisoformat(raw.strip())
    except ValueError:
        logger.warning(
            "tpcore.validation.fundamentals_completeness.bad_horizon_env",
            env=_FUNDAMENTALS_HORIZON_ENV,
            value=raw,
            fallback=FUNDAMENTALS_COMPLETENESS_HORIZON_DEFAULT.isoformat(),
        )
        return FUNDAMENTALS_COMPLETENESS_HORIZON_DEFAULT

    if parsed > FUNDAMENTALS_COMPLETENESS_HORIZON_DEFAULT:
        # Later than default ⇒ NARROWING (shrinks the failing set) ⇒ REJECT.
        # A forward override could silently mask gaps the gate must catch.
        logger.warning(
            "tpcore.validation.fundamentals_completeness.horizon_override_rejected_narrowing",
            env=_FUNDAMENTALS_HORIZON_ENV,
            value=parsed.isoformat(),
            default=FUNDAMENTALS_COMPLETENESS_HORIZON_DEFAULT.isoformat(),
            reason="override later than default would narrow the gate; ignored",
        )
        return FUNDAMENTALS_COMPLETENESS_HORIZON_DEFAULT

    if parsed != FUNDAMENTALS_COMPLETENESS_HORIZON_DEFAULT:
        # Earlier than default ⇒ WIDENING (safe) ⇒ honoured, but logged LOUD
        # so any active shift from the documented default is operator-visible.
        logger.warning(
            "tpcore.validation.fundamentals_completeness.horizon_override_widened",
            env=_FUNDAMENTALS_HORIZON_ENV,
            value=parsed.isoformat(),
            default=FUNDAMENTALS_COMPLETENESS_HORIZON_DEFAULT.isoformat(),
        )
    return parsed


# Module-level convenience constant (the resolved horizon at import time;
# the live evaluation always calls :func:`_resolve_horizon` so an env
# override mid-process is honoured). Exported for tests + downstream.
FUNDAMENTALS_COMPLETENESS_HORIZON: date = FUNDAMENTALS_COMPLETENESS_HORIZON_DEFAULT

# ── Non-operating-entity routing (2026-06-07, sentinel-refinement) ─────
# An anchored=False issuer whose ``asset_class`` is one of these has NO
# 10-Q / 10-K periodic-report obligation (funds / ETFs / ETNs file N-1A /
# N-CSR etc., not operating-company periodic reports). It is EXCLUDED-WITH-
# EVIDENCE (the evidence is ``asset_class ∈ {etf, etn, fund}`` ⇒ no periodic
# obligation) to a NEW ``excluded_non_filer`` bucket that does NOT count in
# the metadata-coverage sentinel numerator/denominator — these are NOT real
# coverage gaps, so counting them produces a false sentinel signal.
#
# CONSERVATIVE / FAIL-CLOSED: only these explicit non-operating classes are
# excluded. An anchored=False OPERATING company (asset_class ∈ {stock, reit})
# that lacks metadata STAYS ``excluded_metadata_required`` (a REAL coverage
# gap, sentinel-visible). A NULL / ambiguous / unrecognised asset_class is
# NOT classified non-operating — it defaults to metadata_required
# (sentinel-visible). SPACs (asset_class='spac') are NOT in this set: a SPAC
# routes by its normal path (CIK-backed → metadata_required; CIK-less →
# confirmed_data_gap; a SPAC with a real sec_document_type_primary that
# anchors stays in the routed denominator).
#
# Valid asset_class enum (migration 20260530_0100): stock, adr, preferred,
# reit, etf, etn, cef, fund, spac. The {etf, etn, fund} subset are the
# operator-named non-operating-no-10-Q classes for this gate.
_NON_OPERATING_ASSET_CLASSES: frozenset[str] = frozenset({"etf", "etn", "fund"})

# Cadence forms. The check ROUTES on these (base-form of the issuer's
# ``sec_document_type_primary``); any other form value falls into the
# OTHER_FORM exclusion bucket (e.g. ``N-1A`` for closed-end funds — not
# a periodic operating-company filing).
#
# These are DERIVED from the shared store's routed-form SoT
# (``ROUTED_QUARTERLY_FORMS`` / ``ROUTED_ANNUAL_FORMS``, /A collapsed via
# ``base_form``) so the CHECK constraint, the validator routing, and the
# store's expected-period filter can never drift. The parity test
# ``tpcore/quality/tests/test_sec_periodic_filings_store.py
# ::test_routed_forms_equal_validator_routing`` asserts the equality.
#
# Note: amendment variants (``10-Q/A``, ``10-K/A``, …) collapse to base
# here — the P0 ``extract_filing_metadata`` primitive collapses ``/A``
# amendments to their base form for primary classification, so a
# 10-Q/A-only filer's primary form lands as ``10-Q``; this routing set
# is correct.
_QUARTERLY_FORMS: frozenset[str] = frozenset(
    base_form(f) for f in _STORE_ROUTED_QUARTERLY_FORMS
)
_ANNUAL_FORMS: frozenset[str] = frozenset(
    base_form(f) for f in _STORE_ROUTED_ANNUAL_FORMS
)
_ROUTED_FORMS: frozenset[str] = _QUARTERLY_FORMS | _ANNUAL_FORMS

# Quarterly cadence — Q4 is 92 days + 8-day late-filing slack. Retained
# only for the per-cadence liveness gate + reporting (the gap math is now
# the authoritative SEC reportDate set-difference, NOT a day-gap cap).
MAX_QUARTERLY_GAP_DAYS = 100

# Annual cadence — 365 + 4-month 20-F deadline + ~30-day late-filing
# slack. Retained only for liveness/reporting (see MAX_QUARTERLY_GAP_DAYS).
MAX_ANNUAL_GAP_DAYS = 450

# Per-cadence liveness gates. The pre-P1 single 120-day window silently
# excluded annual filers (a 20-F filer just past their 4-month deadline
# looks "dark" by quarterly standards). Each form gets its own window.
LIVE_WITHIN_DAYS_QUARTERLY = 120
LIVE_WITHIN_DAYS_ANNUAL = 540

# CONFIRMED_DATA_GAP routing (P3 set-difference rewrite): the new-listing
# grace window is no longer needed — a brand-new listing is ``anchored=True``
# (it HAS sec_periodic_filings rows) with no missing reportDates, so it
# PASSES naturally. The CONFIRMED_DATA_GAP bucket now collects ``anchored=
# False`` CIK-less issuers (no SEC obligation we can verify) + dual-source-
# evidenced empty periods (the evidence-join path below).

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
_ZERO_ANCHORED_SENTINEL_TICKER = "<zero_anchored_universe>"

# P2b (2026-05-31): terminal lifecycle states from the issuer-lifecycle
# evidence model (migration 20260530_0300; populated by the
# ``backfill_sec_lifecycle`` stage). A ticker in any of these states
# has SEC Form 25 / Form 15 evidence of termination — the validator
# routes them to ``excluded_lifecycle_terminated`` instead of the
# silence-based ``excluded_dark`` heuristic (evidence > heuristic).
_TERMINAL_LIFECYCLE_STATES: frozenset[str] = frozenset({
    "deregistered", "delist_effective",
})

# `excluded_confirmed_data_gap` validator-semantics extension
# (2026-06-03, spec PR #450 + plan PR #451).
#
# CONFIRMED_DATA_GAP_FRESHNESS_DAYS — evidence rows older than this
# fall back to FAIL (re-attempt window). 180 days = ~2 fiscal
# quarters; matches the plan's operator-resolved decision.
CONFIRMED_DATA_GAP_FRESHNESS_DAYS = 180

# ARDT_WATCHLIST — operator override per plan §11. ARDT's FMP rows
# are structurally rejected by the `physical_truth` gate (a real
# defect being triaged separately). Until the underlying FMP issue
# is resolved, ARDT must NOT be allowed to qualify for the extended
# dual-source-evidence exclusion (even if the dual-source evidence
# accrues, FMP's `empty` here doesn't mean source-unavailable — it
# means physical_truth rejected the row). The watchlist forces ARDT
# into `excluded_dark` instead.
#
# Module-level constant per plan §17 #2 (small surface; revisitable).
# Future-work TODO: make this dynamic, sourcing from a
# `ticker_quality_overrides` or similar substrate.
ARDT_WATCHLIST: frozenset[str] = frozenset({"ARDT"})

# Evidence-join SQL — Plan 2 reads confirmed-data-gap evidence from
# `platform.data_quality_log` (kind='confirmed_data_gap_evidence'); the standalone
# `fundamentals_period_source_evidence` table was dropped in migration 0300. The
# join SQL is the single shared fragment in
# `tpcore.quality.confirmed_data_gap_store.EVIDENCE_JOIN_SQL`, which preserves the
# plan §8 dual-source EXCLUSION semantics EXACTLY (freshness gate at 180 days via
# `CONFIRMED_DATA_GAP_FRESHNESS_DAYS`; ≥1 `fmp_*` + ≥1 `sec_companyfacts` leg both
# `empty`/`extract_none`; hard-reject the period if any leg is `fetch_failure`).
_EVIDENCE_JOIN_SQL = _CONFIRMED_DATA_GAP_EVIDENCE_JOIN_SQL


# Routed universe — tier≤2 issuers with their identity (classification_id
# + CIK) and routing metadata. This is the DENOMINATOR universe: it is
# anchored on ``ticker_classifications`` (NOT on ``fundamentals_quarterly``)
# so an issuer with ZERO fundamentals rows still appears — that is the
# ``anchored=False`` surface the set-difference gate must judge (a CIK-less
# name → confirmed_data_gap; a CIK-backed name with no SEC substrate yet →
# metadata_required). LEFT JOIN fundamentals so present period_end_dates
# come along for the per-cadence liveness gate + reporting.
#
# ``tc.id`` is the canonical classification_id (text; FK target of
# ``sec_periodic_filings.classification_id`` + ``fundamentals_quarterly
# .classification_id``) — the shared store keys on it.
_FILING_DATES_SQL = """
    WITH liquid AS (
        SELECT DISTINCT ON (tc.ticker)
               tc.ticker,
               tc.id AS classification_id,
               tc.cik,
               tc.asset_class,
               tc.sec_document_type_primary,
               tc.issuer_lifecycle_state,
               tc.issuer_lifecycle_event_date
        FROM platform.liquidity_tiers lt
        JOIN platform.ticker_classifications tc ON tc.ticker = lt.ticker
        WHERE lt.tier <= $1
          AND (tc.lifetime_end IS NULL OR tc.lifetime_end > CURRENT_DATE)
        ORDER BY tc.ticker, tc.lifetime_start DESC NULLS LAST
    )
    SELECT liquid.ticker,
           liquid.classification_id,
           liquid.cik,
           liquid.asset_class,
           liquid.sec_document_type_primary,
           liquid.issuer_lifecycle_state,
           liquid.issuer_lifecycle_event_date,
           fq.period_end_date
    FROM liquid
    LEFT JOIN platform.fundamentals_quarterly fq
        ON fq.ticker = liquid.ticker
       AND fq.period_end_date IS NOT NULL
    ORDER BY liquid.ticker, fq.period_end_date
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
    # P2b (2026-05-31) — evidence-backed terminal state (Form 25 /
    # Form 15 from issuer-lifecycle backfill). Disjoint from
    # excluded_dark: tickers with terminal evidence are bucketed here
    # FIRST; only tickers WITHOUT lifecycle evidence fall through to
    # the silence-based dark heuristic.
    excluded_lifecycle_terminated: int = 0
    # `excluded_confirmed_data_gap` sub-counter (2026-06-03). Logged
    # via structlog at completion; the parent counter
    # ``excluded_confirmed_data_gap`` always carries the total
    # (sparse + evidenced) so existing readers see the right number.
    excluded_confirmed_data_gap_evidenced: int = 0
    # Non-operating-entity bucket (2026-06-07). An anchored=False issuer
    # whose asset_class ∈ {etf, etn, fund} has NO 10-Q/10-K periodic-report
    # obligation → excluded-WITH-evidence here, and explicitly OUT of the
    # metadata-coverage sentinel numerator/denominator (it is not a real
    # coverage gap).
    excluded_non_filer: int = 0
    # Identity-contradiction counter (safety review #1). Count of issuers
    # that are anchored=True (REAL SEC periodic filings) yet whose
    # asset_class label is one of the non-operating classes ({etf, etn,
    # fund}). An "etf" that files 10-Qs is an identity contradiction
    # (asset_class is OpenFIGI-derived and NOT authoritative; a misclassified
    # operating filer). Such issuers are evaluated NORMALLY (gaps FAIL) — the
    # contradiction is NOT masked. This counter makes the contradiction
    # operator-visible (it never affects PASS/FAIL on its own).
    non_operating_anchored_contradiction: int = 0
    # ≥2016 horizon bucket (2026-06-07). Total count of expected SEC
    # report_dates that fell BEFORE the horizon across all anchored routed
    # issuers — excluded-WITH-evidence (the reportDate exists in
    # sec_periodic_filings; excluded by documented horizon policy). Surfaced
    # + logged; NEVER a per-ticker FAIL.
    excluded_pre_horizon: int = 0
    by_form: dict[str, int] = field(default_factory=dict)
    # ticker → (sorted list of SEC-filed reportDates absent from
    # fundamentals_quarterly — the authoritative set-difference, form)
    gaps: dict[str, tuple[list[date], str]] = field(default_factory=dict)
    # Set when the metadata-coverage sentinel must additionally fire.
    metadata_coverage_low: bool = False
    metadata_coverage_ratio: float = 0.0
    # Set when the routed universe anchored ZERO issuers yet excluded some
    # (ANY exclusion bucket non-empty) — a structural FAIL (safety review #2):
    # a routed universe with no anchored evidence must NEVER be GREEN, even
    # when coverage_ratio (which omits confirmed_data_gap + the other
    # exclusion buckets) reads 0.0. The guard sums ALL exclusion buckets so a
    # universe that is entirely excluded_non_filer / excluded_pre_horizon /
    # excluded_dark / … can never read GREEN by emptying the failing set.
    zero_anchored_with_exclusions: bool = False


def _cadence_for(primary_form: str | None) -> tuple[str, int, int] | None:
    """Return (cadence_name, max_gap_days, live_within_days) for the
    given primary form, or None if not routable.

    ``cadence_name`` ("quarterly"|"annual") is what the shared store's
    ``compute_filing_gaps`` keys on. ``max_gap_days`` is retained for the
    failure-message wording; ``live_within_days`` drives the per-cadence
    liveness (dark) gate. The form is matched on its BASE form (``/A``
    amendments collapse to base) so a primary that arrived as ``10-Q/A``
    still routes quarterly.
    """
    base = base_form(primary_form) if primary_form is not None else None
    if base in _QUARTERLY_FORMS:
        return ("quarterly", MAX_QUARTERLY_GAP_DAYS, LIVE_WITHIN_DAYS_QUARTERLY)
    if base in _ANNUAL_FORMS:
        return ("annual", MAX_ANNUAL_GAP_DAYS, LIVE_WITHIN_DAYS_ANNUAL)
    return None


async def _evaluate(pool: asyncpg.Pool) -> _Evaluation:
    """Run the invariant once. Single source of truth for both
    ``check_fundamentals_quarterly_completeness`` (detection) and
    ``compute_fundamentals_repair_targets`` (healing) — they cannot
    disagree because they are the same code."""
    today = datetime.now(UTC).date()

    async with pool.acquire() as conn:
        rows = await conn.fetch(_FILING_DATES_SQL, TRADEABLE_TIER_MAX)
        # Plan 2: confirmed-data-gap evidence now lives in
        # `platform.data_quality_log` (kind='confirmed_data_gap_evidence');
        # the standalone `fundamentals_period_source_evidence` table was
        # dropped in migration 0300. The dql table is a permanent fixture, so
        # the old `to_regclass` existence probe is gone — the evidence join
        # always runs.

    if not rows:
        return _Evaluation(
            sentinel=FailureDetail(
                ticker=_UNIVERSE_SENTINEL_TICKER,
                reason="empty_liquid_universe",
                expected=(
                    f"tier≤{TRADEABLE_TIER_MAX} active issuer to resolve "
                    f"from liquidity_tiers ⋈ ticker_classifications"
                ),
                observed=(
                    "zero active T1/T2 issuers resolved — "
                    "liquidity_tiers / ticker_classifications empty or stale"
                ),
            ),
            evaluated_routed=0, excluded_dark=0,
            excluded_metadata_required=0,
            excluded_confirmed_data_gap=0,
            excluded_other_form=0,
        )

    # Group rows by ticker; capture each ticker's identity
    # (classification_id, CIK) + routing metadata. Each ticker is one row
    # in ``liquid`` (DISTINCT ON), LEFT JOINed to fundamentals — so a
    # period_end_date of NULL means the issuer has ZERO fundamentals rows.
    per_ticker: dict[str, list[date]] = {}
    cid_by_ticker: dict[str, str | None] = {}
    cik_by_ticker: dict[str, str | None] = {}
    asset_class_by_ticker: dict[str, str | None] = {}
    primary_by_ticker: dict[str, str | None] = {}
    lifecycle_by_ticker: dict[str, str | None] = {}
    for r in rows:
        ticker = r["ticker"]
        pe = r["period_end_date"]
        bucket = per_ticker.setdefault(ticker, [])
        if pe is not None:  # LEFT JOIN may yield a NULL placeholder row
            bucket.append(pe)
        cid_by_ticker[ticker] = r["classification_id"]
        cik_by_ticker[ticker] = r.get("cik")
        asset_class_by_ticker[ticker] = r.get("asset_class")
        primary_by_ticker[ticker] = r["sec_document_type_primary"]
        # P2b: ``issuer_lifecycle_state`` is NULL until the lifecycle
        # backfill stage runs against this ticker. NULL → fall through
        # to the silence-based excluded_dark heuristic; a known terminal
        # state short-circuits BEFORE the cadence check.
        lifecycle_by_ticker[ticker] = r.get("issuer_lifecycle_state")

    evaluated_routed = 0
    excluded_dark = 0
    excluded_metadata_required = 0
    excluded_confirmed_data_gap = 0
    excluded_confirmed_data_gap_evidenced = 0
    excluded_other_form = 0
    excluded_lifecycle_terminated = 0
    excluded_non_filer = 0
    non_operating_anchored_contradiction = 0
    excluded_pre_horizon = 0
    horizon = _resolve_horizon()
    by_form: dict[str, int] = {}
    gaps: dict[str, tuple[list[date], str]] = {}

    # ── Pass 1: bucket each ticker, collecting routed candidates ───────
    # A routed candidate is a (ticker, classification_id, cadence_name,
    # primary) that survives the lifecycle / metadata / other-form /
    # liveness pre-filters; its gap is then computed authoritatively
    # against ``platform.sec_periodic_filings`` by the shared store.
    routed: list[tuple[str, str, str, str]] = []  # (ticker, cid, cadence, form)
    cadence_by_cid: dict[str, str] = {}
    # Non-operating asset_class set, captured in Pass 1 for the
    # anchored-gated non_filer decision in Pass 2 (safety review #1).
    non_operating_by_ticker: dict[str, bool] = {}

    for ticker, period_ends in per_ticker.items():
        # P2b: evidence-first routing. Form 25 / Form 15 evidence of
        # termination is dispositive — route BEFORE cadence/liveness.
        lifecycle_state = lifecycle_by_ticker.get(ticker)
        if lifecycle_state in _TERMINAL_LIFECYCLE_STATES:
            excluded_lifecycle_terminated += 1
            continue

        # Non-operating-entity classification (2026-06-07, CHANGE 2;
        # ANCHORED-GATED 2026-06-07 safety review #1). asset_class ∈
        # {etf, etn, fund} has NO 10-Q/10-K periodic-report obligation IF the
        # issuer truly is non-operating — but asset_class is OpenFIGI-derived
        # and NOT authoritative (documented misclassification precedent). The
        # dispositive signal of "operating filer" is ``anchored`` (HAS real
        # SEC periodic filings), which is only known AFTER
        # ``compute_filing_gaps``. So we MUST NOT short-circuit to
        # ``excluded_non_filer`` here in Pass 1: a misclassified OPERATING
        # filer (asset_class wrongly 'etf' but anchored=True with real 10-Qs
        # and real ≥2016 gaps) would be masked before evidence is consulted.
        #
        # The non_filer decision is DEFERRED to Pass 2 (where ``anchored`` is
        # known): an anchored=False non-operating-asset_class issuer →
        # excluded_non_filer; an anchored=True one is an identity
        # CONTRADICTION (an "etf" that files 10-Qs) that must SURFACE and be
        # evaluated normally (gaps FAIL), counted in
        # ``non_operating_anchored_contradiction``.
        #
        # FAIL-CLOSED: only the explicit non-operating classes are flagged.
        # A NULL / ambiguous / unrecognised asset_class is NOT flagged
        # non-operating — it can never route to non_filer.
        asset_class = (asset_class_by_ticker.get(ticker) or "").strip().lower()
        is_non_operating = asset_class in _NON_OPERATING_ASSET_CLASSES
        non_operating_by_ticker[ticker] = is_non_operating

        primary = primary_by_ticker.get(ticker)
        cadence = _cadence_for(primary)
        if cadence is None:
            # The form is not routable, so this issuer can never reach Pass 2
            # (where anchored is computed). Resolve its bucket here:
            #   * NULL primary + non-operating asset_class → excluded_non_filer
            #     (it has no routable form AND no operating obligation; the
            #     asset_class is the only evidence, and there is no anchored
            #     signal to contradict it). This preserves the live-data
            #     behaviour: the dominant non-operating population has a NULL
            #     primary and would otherwise inflate metadata_required.
            #   * NULL primary + operating/ambiguous asset_class →
            #     METADATA_REQUIRED (a real coverage gap, sentinel-visible).
            #   * Any non-NULL non-routed form (e.g. ``N-1A``) → OTHER_FORM.
            if primary is None:
                if is_non_operating:
                    excluded_non_filer += 1
                else:
                    excluded_metadata_required += 1
            else:
                excluded_other_form += 1
            continue

        cadence_name, _max_gap, live_within = cadence

        # Per-cadence liveness gate. A ticker silent past the cadence
        # window is dark and excluded BEFORE the set-difference (we do
        # not demand SEC-filed periods from an issuer that has gone
        # silent). Tickers with no fundamentals rows (period_ends empty)
        # have no last-filed anchor — they fall through to the gap
        # compute, where ``anchored`` discriminates substrate-present
        # (genuine gap on every filed period) from substrate-absent.
        if period_ends and (today - period_ends[-1]).days > live_within:
            excluded_dark += 1
            continue

        cid = cid_by_ticker.get(ticker)
        if cid is None:
            # A tier≤2 routed ticker with NO classification_id is an
            # identity defect (every fundamentals/SEC row FKs to
            # ticker_classifications.id); treat as METADATA_REQUIRED so
            # it surfaces via the coverage sentinel rather than passing.
            excluded_metadata_required += 1
            continue

        routed.append((ticker, cid, cadence_name, primary))
        cadence_by_cid[cid] = cadence_name

    # ── Authoritative SEC reportDate set-difference (shared store) ─────
    # ONE set-based call over the whole routed universe. The store keys
    # on classification_id, filters expected periods to the cadence's
    # routed forms, and returns per-cid (anchored, missing_periods).
    gap_by_cid: dict[str, Any] = {}
    if routed:
        cids = [cid for _t, cid, _c, _f in routed]
        async with pool.acquire() as conn:
            gap_by_cid = await compute_filing_gaps(
                conn, cids, cadence_by_cid, horizon=horizon,
            )

    # ── Pass 2: verdict mapping (the false-green-critical part) ────────
    for ticker, cid, _cadence_name, primary in routed:
        result = gap_by_cid.get(cid)

        if result is None or not result.anchored:
            # anchored=False ⇒ ZERO sec_periodic_filings rows for this
            # issuer. There is NO SEC periodic evidence to verify
            # against, so we MUST NOT pass and MUST NOT fabricate a gap.
            #
            # NON-OPERATING gate (safety review #1): an anchored=False issuer
            # whose asset_class ∈ {etf, etn, fund} has no operating-company
            # periodic obligation AND no anchored evidence to contradict that
            # label ⇒ excluded_non_filer (excluded-WITH-evidence: the
            # asset_class is the evidence). This is the ONLY place a routed
            # issuer becomes non_filer — it requires BOTH the non-operating
            # label AND anchored=False. (An anchored=True non-operating-label
            # issuer falls through to the contradiction path below.)
            if non_operating_by_ticker.get(ticker, False):
                excluded_non_filer += 1
                continue
            #
            # CIK-based routing for OPERATING / ambiguous issuers:
            #   * CIK-less  ⇒ no SEC obligation we can verify ⇒
            #     excluded-WITH-evidence (confirmed_data_gap).
            #   * CIK-backed ⇒ the periodic-filings backfill simply
            #     hasn't populated yet ⇒ METADATA_REQUIRED (surfaces via
            #     the coverage sentinel; never a silent green).
            #
            # Empty-string-CIK routing (safety review #2): a corrupt
            # ``cik=''`` (or whitespace) is FALSY but is NOT the same as a
            # genuinely CIK-less issuer — it is an identity DEFECT that must
            # be sentinel-VISIBLE. So ``confirmed_data_gap`` (sentinel-blind)
            # is reserved for the genuinely-CIK-less case (the column value
            # is None); a present-but-empty CIK value routes to
            # METADATA_REQUIRED (the identity-defect-visible bucket, fed to
            # the coverage sentinel) alongside the CIK-backed-not-yet-backfilled
            # case. ``cik`` (stripped, present) keeps the normal CIK-backed path.
            raw_cik = cik_by_ticker.get(ticker)
            cik = (raw_cik or "").strip()
            if cik or raw_cik is not None:
                # CIK-backed (valid) OR present-but-empty/whitespace (defect):
                # both surface via the metadata-coverage sentinel.
                excluded_metadata_required += 1
            else:
                # Genuinely CIK-less (no column value): no SEC obligation we
                # can verify ⇒ excluded-with-evidence (confirmed_data_gap).
                excluded_confirmed_data_gap += 1
            continue

        # anchored=True ⇒ this issuer IS in the denominator.
        evaluated_routed += 1
        by_form[primary] = by_form.get(primary, 0) + 1

        # Identity-contradiction surface (safety review #1): an anchored=True
        # issuer whose asset_class label is non-operating ({etf, etn, fund})
        # is an "etf" that files 10-Qs — a misclassification (asset_class is
        # OpenFIGI-derived, not authoritative). It is evaluated NORMALLY here
        # (real filings ⇒ real gaps FAIL); the contradiction is NOT masked.
        # We only TALLY it so the operator can see it (never affects PASS/FAIL).
        if non_operating_by_ticker.get(ticker, False):
            non_operating_anchored_contradiction += 1

        # ≥2016 horizon (CHANGE 1): the store already removed pre-horizon
        # expected reportDates from ``missing_periods``; accumulate the
        # per-issuer excluded count for the surfaced ``excluded_pre_horizon``
        # bucket (excluded-WITH-evidence, logged below). This is purely a
        # diagnostic tally — it never affects PASS/FAIL.
        excluded_pre_horizon += result.excluded_pre_horizon

        ticker_gaps = list(result.missing_periods)
        if not ticker_gaps:
            continue  # SEC evidence present + fundamentals complete ⇒ PASS

        # ARDT override (per plan §11): ARDT's FMP rows are rejected by
        # physical_truth — the FMP `empty` is gate-rejected, not
        # source-unavailable. Force ARDT into `excluded_dark` BEFORE
        # consulting the evidence join. (Undo the evaluated_routed /
        # by_form increments above — it routes to an exclusion bucket.)
        if ticker in ARDT_WATCHLIST:
            excluded_dark += 1
            evaluated_routed -= 1
            by_form[primary] = max(0, by_form.get(primary, 0) - 1)
            continue

        # Evidence join: route dual-source-confirmed-empty periods to
        # `excluded_confirmed_data_gap_evidenced`. The remaining
        # un-evidenced periods stay in the ticker's gap list → ticker
        # FAILs on those. The freshness gate (180 days) + fetch_failure
        # rejection are enforced inside the SQL.
        async with pool.acquire() as conn:
            ev_rows = await conn.fetch(
                _EVIDENCE_JOIN_SQL, ticker, sorted(ticker_gaps),
                CONFIRMED_DATA_GAP_FRESHNESS_DAYS,
            )
        evidenced = {r["period_end_date"] for r in ev_rows}
        if evidenced:
            excluded_confirmed_data_gap_evidenced += len(evidenced)
            excluded_confirmed_data_gap += len(evidenced)
            ticker_gaps = [d for d in ticker_gaps if d not in evidenced]

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

    # Zero-anchored-universe structural guard (safety review #1 + #2). A routed
    # universe that anchored ZERO issuers (``evaluated_routed == 0``) while ANY
    # issuer was excluded (ANY bucket non-empty) is NOT a vacuous PASS — there
    # is no green SEC evidence anywhere, only exclusions. The coverage ratio
    # above is computed on a denominator that OMITS confirmed_data_gap (and the
    # other buckets), so a universe that is 100% confirmed_data_gap — or 100%
    # excluded_non_filer, or 100% excluded_pre_horizon — has coverage_ratio ==
    # 0.0 and would slip through as GREEN. Sum ALL exclusion buckets so a
    # routed-but-fully-unanchored universe can NEVER be GREEN regardless of
    # which bucket the exclusions landed in.
    excluded_total = (
        excluded_metadata_required
        + excluded_confirmed_data_gap
        + excluded_non_filer
        + excluded_dark
        + excluded_lifecycle_terminated
        + excluded_other_form
        + excluded_pre_horizon
    )
    zero_anchored_with_exclusions = (
        evaluated_routed == 0 and excluded_total > 0
    )

    return _Evaluation(
        sentinel=None,
        evaluated_routed=evaluated_routed,
        excluded_dark=excluded_dark,
        excluded_metadata_required=excluded_metadata_required,
        excluded_confirmed_data_gap=excluded_confirmed_data_gap,
        excluded_other_form=excluded_other_form,
        excluded_lifecycle_terminated=excluded_lifecycle_terminated,
        excluded_confirmed_data_gap_evidenced=(
            excluded_confirmed_data_gap_evidenced
        ),
        excluded_non_filer=excluded_non_filer,
        non_operating_anchored_contradiction=(
            non_operating_anchored_contradiction
        ),
        excluded_pre_horizon=excluded_pre_horizon,
        by_form=by_form,
        gaps=gaps,
        metadata_coverage_low=metadata_coverage_low,
        metadata_coverage_ratio=coverage_ratio,
        zero_anchored_with_exclusions=zero_anchored_with_exclusions,
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

    # Zero-anchored-universe structural sentinel (safety review #1) —
    # prepended FIRST: a routed universe that anchored zero issuers while
    # excluding some is a vacuous-pass risk and must hard-FAIL. Emitted
    # before the metadata-coverage sentinel because it is the more
    # fundamental "no green evidence anywhere" condition.
    if ev.zero_anchored_with_exclusions:
        excluded_total = (
            ev.excluded_metadata_required
            + ev.excluded_confirmed_data_gap
            + ev.excluded_non_filer
            + ev.excluded_dark
            + ev.excluded_lifecycle_terminated
            + ev.excluded_other_form
            + ev.excluded_pre_horizon
        )
        failures.append(FailureDetail(
            ticker=_ZERO_ANCHORED_SENTINEL_TICKER,
            reason="zero_anchored_universe",
            expected=(
                "≥ 1 routed issuer ANCHORED on platform.sec_periodic_filings "
                "(SEC periodic evidence to verify against)"
            ),
            observed=(
                f"zero anchored issuers in the routed universe while "
                f"{excluded_total} were excluded "
                f"(metadata_required={ev.excluded_metadata_required}, "
                f"confirmed_data_gap={ev.excluded_confirmed_data_gap}, "
                f"non_filer={ev.excluded_non_filer}, "
                f"dark={ev.excluded_dark}, "
                f"lifecycle_terminated={ev.excluded_lifecycle_terminated}, "
                f"other_form={ev.excluded_other_form}, "
                f"pre_horizon={ev.excluded_pre_horizon}) — "
                f"no green SEC evidence anywhere; extend the SEC "
                f"periodic-filings backfill before trusting this gate"
            ),
        ))

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

    # Per-ticker cadence failures. ``missing`` is now the AUTHORITATIVE
    # set-difference (SEC-filed reportDates absent from fundamentals) —
    # every named date is a reportDate the SEC literally filed, not an
    # interpolated estimate.
    for ticker, (missing, form) in sorted(ev.gaps.items()):
        cadence_name, _max_gap, _live = _cadence_for(form) or (
            "unknown", 0, 0
        )
        shown = ", ".join(d.isoformat() for d in missing[:8])
        more = "" if len(missing) <= 8 else f" (+{len(missing) - 8} more)"
        failures.append(FailureDetail(
            ticker=ticker,
            reason=f"missing_period_{form}",
            expected=(
                f"fundamentals_quarterly to carry every SEC-filed "
                f"reportDate (cadence={cadence_name}, form={form})"
            ),
            observed=(
                f"{len(missing)} SEC-filed reportDate(s) missing from "
                f"fundamentals_quarterly: {shown}{more}"
            ),
        ))

    total_failed = len(failures)
    passed = total_failed == 0

    # Per plan §9, log the sparse-vs-evidenced split of the
    # `excluded_confirmed_data_gap` bucket so the operator can see at
    # a glance how many exclusions came from the existing sparse-ticker
    # path vs the new dual-source-evidence path.
    sparse_count = (
        ev.excluded_confirmed_data_gap
        - ev.excluded_confirmed_data_gap_evidenced
    )
    if passed:
        logger.info(
            "tpcore.validation.fundamentals_completeness.ok",
            evaluated_routed=ev.evaluated_routed,
            excluded_dark=ev.excluded_dark,
            excluded_lifecycle_terminated=ev.excluded_lifecycle_terminated,
            excluded_metadata_required=ev.excluded_metadata_required,
            excluded_confirmed_data_gap=ev.excluded_confirmed_data_gap,
            excluded_confirmed_data_gap_sparse=sparse_count,
            excluded_confirmed_data_gap_evidenced=(
                ev.excluded_confirmed_data_gap_evidenced
            ),
            excluded_non_filer=ev.excluded_non_filer,
            non_operating_anchored_contradiction=(
                ev.non_operating_anchored_contradiction
            ),
            excluded_pre_horizon=ev.excluded_pre_horizon,
            horizon=_resolve_horizon().isoformat(),
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
            excluded_lifecycle_terminated=ev.excluded_lifecycle_terminated,
            excluded_metadata_required=ev.excluded_metadata_required,
            excluded_confirmed_data_gap=ev.excluded_confirmed_data_gap,
            excluded_confirmed_data_gap_sparse=sparse_count,
            excluded_confirmed_data_gap_evidenced=(
                ev.excluded_confirmed_data_gap_evidenced
            ),
            excluded_non_filer=ev.excluded_non_filer,
            non_operating_anchored_contradiction=(
                ev.non_operating_anchored_contradiction
            ),
            excluded_pre_horizon=ev.excluded_pre_horizon,
            horizon=_resolve_horizon().isoformat(),
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


async def compute_fundamentals_gap_periods(
    pool: asyncpg.Pool,
) -> dict[str, list[date]]:
    """Return the validator's current per-ticker missing-period map.

    Public companion to ``compute_fundamentals_repair_targets`` —
    same ``_evaluate`` source, returns ``ticker → sorted missing
    period_end_dates`` instead of just the ticker list. Used by the
    ``confirmed_data_gap_evidence_populator`` stage to scope its
    per-period FMP+SEC attempts to exactly the periods the validator
    is FAILing on.

    Returns ``{}`` when the validator has no gaps OR a structural
    sentinel is active (those aren't bars-backfill-fixable — see the
    repair-targets companion).
    """
    ev = await _evaluate(pool)
    if ev.sentinel is not None or not ev.gaps:
        return {}
    return {t: sorted(missing) for t, (missing, _form) in ev.gaps.items()}


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
    "ARDT_WATCHLIST",
    "CHECK_NAME",
    "CONFIRMED_DATA_GAP_FRESHNESS_DAYS",
    "FUNDAMENTALS_COMPLETENESS_HORIZON",
    "FUNDAMENTALS_COMPLETENESS_HORIZON_DEFAULT",
    "LIVE_WITHIN_DAYS_ANNUAL",
    "LIVE_WITHIN_DAYS_QUARTERLY",
    "MAX_ANNUAL_GAP_DAYS",
    "MAX_QUARTERLY_GAP_DAYS",
    "METADATA_COVERAGE_FAIL_THRESHOLD",
    "TRADEABLE_TIER_MAX",
    "check_fundamentals_quarterly_completeness",
    "compute_fundamentals_gap_periods",
    "compute_fundamentals_repair_targets",
]

# Re-exported for tests + downstream consumers (2026-06-07).
NON_OPERATING_ASSET_CLASSES: frozenset[str] = _NON_OPERATING_ASSET_CLASSES


# Re-exported for tests + downstream consumers (P2b 2026-05-31).
TERMINAL_LIFECYCLE_STATES: frozenset[str] = _TERMINAL_LIFECYCLE_STATES
