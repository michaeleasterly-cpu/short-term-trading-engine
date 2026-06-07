"""Shared substrate store for ``platform.sec_periodic_filings``.

This is the single read/write surface that the SEC periodic-filings
producer (``scripts/ops.py::_stage_backfill_sec_metadata`` →
``tpcore.sec.companyfacts_adapter.extract_filing_metadata``'s
``periodic_filings`` key) and the fundamentals-completeness consumer
(``tpcore/quality/validation/checks/fundamentals_quarterly_completeness``)
share. Centralising the routed-form SoT + the gap computation here means
the CHECK-constraint form set, the validator's cadence routing, and the
INSERT column list can never drift across call sites. It mirrors the
shape of ``tpcore/quality/confirmed_data_gap_store.py`` (the precedent
shared-store module).

# Routed forms (single source of truth)

``ROUTED_QUARTERLY_FORMS`` + ``ROUTED_ANNUAL_FORMS`` (base + ``/A``) and
their ``ROUTED_FORMS`` union MUST equal:

  * the migration ``20260607_0200`` ``form_type`` CHECK set
    (``{10-Q,10-K,20-F,40-F}`` + their ``/A`` variants), AND
  * the fundamentals-completeness check's cadence routing
    (``_QUARTERLY_FORMS`` = {10-Q}; ``_ANNUAL_FORMS`` = {10-K,20-F,40-F}).

A parity test (``tpcore/quality/tests/test_sec_periodic_filings_store.py``)
asserts this equality so the three definitions stay in lock-step.

Note the cadence asymmetry vs the validator: the validator routes on the
issuer's *primary* form (``sec_document_type_primary``, with ``/A``
collapsed to base), whereas this store's per-cadence form filters are
used to scope the ``expected`` periods query (``form_type = ANY(...)``)
and INCLUDE the ``/A`` amendment variants — an amendment is still a
periodic filing for that cadence and its ``report_date`` is set-difference
neutral against the base filing (both sides are DISTINCT).

# Anchored vs missing (false-green protection)

``FilingGapResult.anchored`` is the dispositive discriminator:

  * ``anchored=False`` ⇒ this issuer has ZERO ``sec_periodic_filings``
    rows. There is no SEC periodic evidence, so the caller MUST NOT treat
    the issuer as PASS — it routes through the evidence-exclusion path
    (METADATA_REQUIRED / CONFIRMED_DATA_GAP), never a silent green.
  * ``anchored=True`` + ``missing_periods=()`` ⇒ SEC evidence exists and
    ``fundamentals_quarterly`` has every expected period ⇒ PASS.
  * ``anchored=True`` + ``missing_periods=(...)`` ⇒ SEC says these
    ``report_date`` periods should exist but ``fundamentals_quarterly``
    lacks them ⇒ those periods are the genuine gap.

``anchored=False`` and ``anchored=True, missing_periods=()`` are
DISTINCT outcomes; collapsing them would re-introduce the false-green the
substrate exists to prevent.
"""
from __future__ import annotations

from datetime import date  # runtime import — Pydantic resolves tuple[date, ...]
from typing import TYPE_CHECKING, Literal

import structlog
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable

    import asyncpg

logger = structlog.get_logger(__name__)


# ── Routed-form SoT ────────────────────────────────────────────────────
# These MUST equal the migration 20260607_0200 form_type CHECK set AND the
# fundamentals_quarterly_completeness check's cadence routing. A parity
# test asserts this equality — do NOT edit one definition without the
# others (CHECK constraint + validator + this store move together).
ROUTED_QUARTERLY_FORMS: frozenset[str] = frozenset({"10-Q", "10-Q/A"})
ROUTED_ANNUAL_FORMS: frozenset[str] = frozenset({
    "10-K", "20-F", "40-F",
    "10-K/A", "20-F/A", "40-F/A",
})
ROUTED_FORMS: frozenset[str] = ROUTED_QUARTERLY_FORMS | ROUTED_ANNUAL_FORMS


def base_form(form_type: str) -> str:
    """Collapse an amendment variant to its base form (``10-Q/A`` →
    ``10-Q``). A non-amendment form is returned unchanged."""
    if form_type.endswith("/A"):
        return form_type[:-2]
    return form_type


def _routed_for_cadence(cadence: Literal["quarterly", "annual"]) -> list[str]:
    forms = (
        ROUTED_QUARTERLY_FORMS if cadence == "quarterly"
        else ROUTED_ANNUAL_FORMS
    )
    return sorted(forms)


class FilingGapResult(BaseModel):
    """Per-issuer result of the SEC-periodic-vs-fundamentals gap compute.

    ``anchored`` is the false-green guard (see module docstring):
    ``False`` ⇒ no SEC periodic record exists, caller must NOT PASS;
    ``True`` ⇒ SEC evidence exists and ``missing_periods`` is the genuine
    set-difference (empty ⇒ PASS).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    anchored: bool
    missing_periods: tuple[date, ...]
    routed_forms: frozenset[str]


# ── Writer ─────────────────────────────────────────────────────────────
# Supabase chunked-DML mandate: never a single multi-thousand-row INSERT.
WRITE_CHUNK_SIZE = 500

_INSERT_SQL = """
    INSERT INTO platform.sec_periodic_filings
        (cik, ticker, form_type, report_date, filing_date, accession_number)
    SELECT * FROM unnest(
        $1::text[], $2::text[], $3::text[], $4::date[], $5::date[], $6::text[]
    )
    ON CONFLICT (cik, accession_number) DO NOTHING
"""


async def write_periodic_filings(
    conn_or_pool: asyncpg.Connection | asyncpg.Pool,
    rows: Iterable[object],
    *,
    cik: str,
    ticker: str,
) -> int:
    """Idempotently persist ``PeriodicFiling``-like rows for one issuer.

    Each row must expose ``form_type`` / ``filing_date`` /
    ``report_date`` / ``accession_number`` attributes (the
    ``tpcore.sec.companyfacts_adapter.PeriodicFiling`` model, or any
    duck-typed equivalent). ``classification_id`` is intentionally OMITTED
    from the INSERT — the table's BEFORE-INSERT trigger fills it from
    ``ticker_history`` at ``as_of = filing_date``.

    Writes in ``WRITE_CHUNK_SIZE``-row chunks (Supabase chunked-DML mandate)
    with ``ON CONFLICT (cik, accession_number) DO NOTHING`` — filings are
    immutable, so a re-run is a no-op. Accepts either a single connection
    (e.g. inside an open transaction) or a pool.

    Returns the number of rows SUBMITTED for insert (pre-dedup); the
    ON CONFLICT clause makes the effective row count idempotent at the DB
    level, but the submitted count is the useful caller-side metric.
    """
    materialised = list(rows)
    if not materialised:
        return 0

    ciks: list[str] = []
    tickers: list[str] = []
    form_types: list[str] = []
    report_dates: list[date | None] = []
    filing_dates: list[date] = []
    accessions: list[str] = []
    for r in materialised:
        ciks.append(cik)
        tickers.append(ticker)
        form_types.append(r.form_type)  # type: ignore[attr-defined]
        report_dates.append(r.report_date)  # type: ignore[attr-defined]
        filing_dates.append(r.filing_date)  # type: ignore[attr-defined]
        accessions.append(r.accession_number)  # type: ignore[attr-defined]

    submitted = len(materialised)

    async def _run(conn: asyncpg.Connection) -> None:
        for i in range(0, submitted, WRITE_CHUNK_SIZE):
            sl = slice(i, i + WRITE_CHUNK_SIZE)
            await conn.execute(
                _INSERT_SQL,
                ciks[sl], tickers[sl], form_types[sl],
                report_dates[sl], filing_dates[sl], accessions[sl],
            )

    # asyncpg.Pool exposes .acquire(); a Connection does not.
    if hasattr(conn_or_pool, "acquire"):
        async with conn_or_pool.acquire() as conn:  # type: ignore[union-attr]
            await _run(conn)
    else:
        await _run(conn_or_pool)  # type: ignore[arg-type]

    logger.debug(
        "tpcore.quality.sec_periodic_filings_store.write_periodic_filings",
        cik=cik, ticker=ticker, submitted=submitted,
    )
    return submitted


# ── Gap computation ────────────────────────────────────────────────────
_EXPECTED_SQL = """
    SELECT classification_id, report_date
    FROM platform.sec_periodic_filings
    WHERE classification_id = ANY($1::text[])
      AND report_date IS NOT NULL
      AND form_type = ANY($2::text[])
"""

# Anchored = issuer has ANY sec_periodic_filings row (regardless of
# report_date / form_type). DISTINCT on classification_id so the result
# is one row per anchored issuer.
_ANCHORED_SQL = """
    SELECT DISTINCT classification_id
    FROM platform.sec_periodic_filings
    WHERE classification_id = ANY($1::text[])
"""

# ``have`` is the set of period_end_date values credited toward each cid.
# Two contributing UNION arms:
#   Arm A — the cid is stamped directly on the fundamentals row.
#   Arm B (defensive, recycled-ticker-SAFE) — the fundamentals row carries
#     ``classification_id IS NULL`` but its (ticker, period_end_date) resolves to
#     cid C through C's OWN ``ticker_history`` window, using the SAME half-open
#     predicate the BEFORE-INSERT trigger uses (anchored on period_end_date). This
#     credits a NULL-cid row to the entity that actually held the ticker for that
#     fiscal period. It is period_end-window-scoped — NOT a raw ticker-only match,
#     which would miscredit recycled-ticker (delisted-then-reused) rows to the
#     current holder. On the repaired data (migration 20260607_0300) this arm is a
#     no-op (0 residual NULL rows fall in any period_end window — they are exactly
#     the pre-IPO-window / window-gap rows the trigger could not anchor), but it
#     makes the gate immune to any FUTURE NULL-cid fundamentals row without
#     guessing identity across an entity boundary.
_HAVE_SQL = """
    SELECT classification_id, period_end_date
    FROM platform.fundamentals_quarterly
    WHERE classification_id = ANY($1::text[])
      AND period_end_date IS NOT NULL
    UNION
    SELECT th.classification_id, fq.period_end_date
    FROM platform.fundamentals_quarterly fq
    JOIN platform.ticker_history th
      ON th.ticker = fq.ticker
     AND th.valid_from <= fq.period_end_date
     AND (th.valid_to IS NULL OR fq.period_end_date < th.valid_to)
    WHERE fq.classification_id IS NULL
      AND fq.period_end_date IS NOT NULL
      AND th.classification_id = ANY($1::text[])
"""


# Fiscal-quarter tolerance for the expected-vs-have match. An SEC
# ``report_date`` and a fundamentals ``period_end_date`` for the SAME fiscal
# quarter routinely differ by a handful of days (observed deltas cluster <= 6
# days; the SEC report_date and the vendor period_end_date pick slightly
# different period-end conventions). A 13-week quarter is ~91 days, so a +/-15
# day window can NEVER absorb an adjacent quarter (the nearest distinct quarter
# is >= ~76 days away), but it does collapse the same-quarter convention skew.
_FISCAL_QUARTER_TOLERANCE_DAYS = 15


def _expected_is_satisfied(report_date: date, have: list[date]) -> bool:
    """An expected ``report_date`` is SATISFIED iff some ``have``
    period_end_date lies within +/-``_FISCAL_QUARTER_TOLERANCE_DAYS`` of it.

    Nearest-match within the tolerance window — NOT exact equality — so a
    same-fiscal-quarter date that differs by a few days still counts. A
    report_date whose nearest have-date is outside the window is genuinely
    MISSING (the +/-15 day window cannot reach an adjacent real quarter).
    """
    return any(
        abs((report_date - h).days) <= _FISCAL_QUARTER_TOLERANCE_DAYS
        for h in have
    )


def _pure_gap(
    *,
    anchored: bool,
    expected: set[date],
    have: set[date],
    routed_forms: frozenset[str],
) -> FilingGapResult:
    """Anchored discriminator + fiscal-quarter-tolerant set-difference (no I/O).

    Both ``expected`` and ``have`` are DISTINCT sets, so restatements /
    amendments (same ``report_date``, different ``accession``) are neutral.
    The expected-vs-have match is NOT exact equality: an expected SEC
    ``report_date`` is satisfied if a ``have`` period_end_date exists within the
    SAME fiscal quarter (+/-``_FISCAL_QUARTER_TOLERANCE_DAYS`` days — see
    :func:`_expected_is_satisfied`), which absorbs the same-quarter
    convention skew between SEC and the vendor without ever collapsing two
    adjacent real quarters (~91 days apart).

    ``missing_periods`` is only meaningful when ``anchored`` is True; an
    un-anchored issuer carries an empty tuple AND ``anchored=False`` — the
    caller keys on ``anchored``, never treating an empty ``missing_periods`` as
    a PASS without checking ``anchored``.
    """
    if not anchored:
        return FilingGapResult(
            anchored=False, missing_periods=(), routed_forms=routed_forms,
        )
    have_sorted = sorted(have)
    missing = tuple(
        rd for rd in sorted(expected)
        if not _expected_is_satisfied(rd, have_sorted)
    )
    return FilingGapResult(
        anchored=True, missing_periods=missing, routed_forms=routed_forms,
    )


async def compute_filing_gaps(
    conn: asyncpg.Connection,
    classification_ids: list[str],
    cadence_by_cid: dict[str, str],
) -> dict[str, FilingGapResult]:
    """SET-BASED gap compute over a universe of issuers.

    A few queries over the WHOLE ``classification_ids`` list (NOT a
    per-ticker round-trip) — the fundamentals check iterates thousands of
    issuers, so this is the efficient entrypoint. ``cadence_by_cid`` maps
    each classification_id to ``"quarterly"`` | ``"annual"`` (the caller
    derives it from ``sec_document_type_primary``); a cid absent from the
    map is skipped (un-routable).

    Per cid:
      * ``anchored`` ⇐ a ``sec_periodic_filings`` row exists for the cid.
      * ``expected`` ⇐ DISTINCT ``report_date`` from ``sec_periodic_filings``
        for the cid, filtered to the cadence's routed forms.
      * ``have`` ⇐ DISTINCT ``period_end_date`` from
        ``fundamentals_quarterly`` for the cid.
      * ``missing`` ⇐ ``sorted(expected - have)`` (only when anchored).

    CRITICAL: a cid with zero SEC rows ⇒ ``anchored=False`` (NOT
    ``missing=()``) — the false-green guard.
    """
    out: dict[str, FilingGapResult] = {}
    if not classification_ids:
        return out

    cids = [c for c in classification_ids if c in cadence_by_cid]
    if not cids:
        return out

    # Both cadences share the anchored + have reads; the expected read is
    # cadence-specific (different routed-form filter). Run the two reads
    # that span all cids once, then the per-cadence expected reads.
    anchored_rows = await conn.fetch(_ANCHORED_SQL, cids)
    anchored_set: set[str] = {r["classification_id"] for r in anchored_rows}

    have_rows = await conn.fetch(_HAVE_SQL, cids)
    have_by_cid: dict[str, set[date]] = {}
    for r in have_rows:
        have_by_cid.setdefault(r["classification_id"], set()).add(
            r["period_end_date"]
        )

    expected_by_cid: dict[str, set[date]] = {}
    for cadence in ("quarterly", "annual"):
        cadence_cids = [c for c in cids if cadence_by_cid[c] == cadence]
        if not cadence_cids:
            continue
        routed = _routed_for_cadence(cadence)  # type: ignore[arg-type]
        exp_rows = await conn.fetch(_EXPECTED_SQL, cadence_cids, routed)
        for r in exp_rows:
            expected_by_cid.setdefault(r["classification_id"], set()).add(
                r["report_date"]
            )

    for cid in cids:
        cadence = cadence_by_cid[cid]
        routed = (
            ROUTED_QUARTERLY_FORMS if cadence == "quarterly"
            else ROUTED_ANNUAL_FORMS
        )
        out[cid] = _pure_gap(
            anchored=cid in anchored_set,
            expected=expected_by_cid.get(cid, set()),
            have=have_by_cid.get(cid, set()),
            routed_forms=routed,
        )
    return out


async def compute_filing_gap(
    conn: asyncpg.Connection,
    classification_id: str,
    cadence: Literal["quarterly", "annual"],
) -> FilingGapResult:
    """Single-issuer convenience wrapper over :func:`compute_filing_gaps`."""
    result = await compute_filing_gaps(
        conn, [classification_id], {classification_id: cadence},
    )
    return result.get(
        classification_id,
        FilingGapResult(
            anchored=False,
            missing_periods=(),
            routed_forms=(
                ROUTED_QUARTERLY_FORMS if cadence == "quarterly"
                else ROUTED_ANNUAL_FORMS
            ),
        ),
    )


__all__ = [
    "ROUTED_ANNUAL_FORMS",
    "ROUTED_FORMS",
    "ROUTED_QUARTERLY_FORMS",
    "WRITE_CHUNK_SIZE",
    "FilingGapResult",
    "base_form",
    "compute_filing_gap",
    "compute_filing_gaps",
    "write_periodic_filings",
]
