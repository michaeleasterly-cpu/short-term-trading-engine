"""Identity-substrate consistency gate — the Phase-1.4 BLOCKING gate
(Plan 3 Phase 1).

Spec: ``docs/superpowers/specs/2026-06-04-data-layer-rebuild-design.md`` §4
/ §5.3; identity-path rule (the ``ticker + date → classification_id →
CIK`` chain must be intact). Discovery:
``docs/audits/2026-06-05-identity-build-code-state.md``.

After ``universe_build → issuers_build → ticker_history_reuse_build →
issuer_securities_build`` run IN ORDER, the coordinator runs THIS gate
BEFORE any child load (prices / fundamentals / lifecycle). It asserts the
identity substrate is internally consistent:

  1. **0 sentinel ``lifetime_start = '1900-01-01'``** — the no-sentinel A6
     invariant. Migration ``20260604_0600`` DROPped the old
     ``'1900-01-01'`` DEFAULT (the column is now NOT NULL, no default), so
     a NULL can no longer occur; the surviving rot class is a sentinel
     VALUE written before the DEFAULT was dropped (or by a non-conforming
     loader). No sentinel must survive (A6).
  2. **0 classifications with ``lifetime_start < first_public_filing_date``**
     — the look-ahead rot the rebuild cures: ``lifetime_start`` is anchored
     at the SEC FPFD, so a classification whose start predates its OWN
     ``first_public_filing_date`` (where FPFD is known) is the pre-rebuild
     contamination. FPFD lives on ``ticker_classifications`` (verified live:
     ``issuers`` has no FPFD column), so this is a single-table self-check.
  3. **0 ``ticker_history`` overlaps** — the half-open windows for the same
     TICKER must not overlap ACROSS classifications (G3 reuse). The DB
     EXCLUDE (migration ``20260524_0100``) keys on ``classification_id WITH
     =`` so it ONLY guards same-classification overlap; cross-classification
     same-ticker overlap is NOT a DB invariant — it is guarded by the
     pure-layer ``derive_ticker_history`` hard-stop AND this probe. The
     probe is therefore load-bearing: it must run on every live build.
  4. **0 ``issuer_history`` overlaps** — ``issuer_history`` has NO EXCLUDE
     constraint at all (verified live: only PK + FK), so the half-open
     legal-name windows for the same ``issuer_id`` are guarded ONLY here.
  5. **Every cik-bearing classification has an ``issuers`` row** (issuers_build
     covered every CIK). The cik-join is zfill-10-normalized on BOTH sides
     because the FMP-fallback writer (``scripts/ops.py``, ``cik_source =
     'fmp'``) may land an UNPADDED ``ticker_classifications.cik`` — a raw
     text compare would report a false orphan.
  6. **Every cik-bearing classification has an ``issuer_securities`` link**
     (issuer_securities_build fanned every cik-bearing security out).
  7. **0 orphan ``classification_id``** in ``ticker_history`` /
     ``issuer_securities`` (every referenced classification exists in
     ``ticker_classifications`` — the FK is not yet enforced at the DB level
     for ``ticker_history``).
  8. **0 orphan ``issuer_id``** in ``issuer_securities`` / ``issuer_history``
     (every referenced issuer exists — the M:N + SCD-2 tables point only
     at real issuers).
  9. **Every etf/etn classification has an ``etf_attributes`` row** — the ETF
     satellite (migration ``20260607_0100``, physical-entity separation) is a
     1:1 attribute table for ``asset_class IN ('etf','etn')``. A missing
     satellite row for an ETF/ETN classification is the seed-incompleteness rot
     this probe guards.
  10. **0 ``etf_attributes`` rows whose classification is NOT etf/etn** — the
      satellite must hold ONLY ETF/ETN attributes. A row pointing at a
      stock/spac/fund/adr/reit classification is a mis-scoped write.

This gate runs **12 probes** total (the ``_PROBES`` tuple below is the
authoritative count): the original 10 identity-substrate probes plus the two
ETF-satellite probes (9 + 10) added with migration ``20260607_0100``.

This gate is NOT a member of the 13-check ``DATA_OPERATIONS_COMPLETE``
suite (those checks gate the *child* data tables; the existing
``issuer_history_integrity`` + ``issuer_securities_integrity`` suite checks
+ their HealSpecs cover the steady-state). It is the orchestrator-time
pre-child-load gate — read-only, fail-fast, no auto-heal (a violation here
means the identity build itself is incomplete; re-run the build, do not
patch the substrate).
"""
from __future__ import annotations

from typing import Protocol

import structlog
from pydantic import BaseModel, ConfigDict

logger = structlog.get_logger(__name__)


class _Fetchval(Protocol):
    # The gate's probes are parameterless COUNT queries — ``fetchval(sql)``.
    async def fetchval(self, sql: str) -> int | None: ...


class IdentityGateResult(BaseModel):
    """The gate verdict + the per-probe violation counts (only non-zero
    probes appear in ``violations``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    violations: dict[str, int]


# Each probe: (violation key, SQL returning a COUNT of offending rows).
# A clean substrate returns 0 for every probe.

# (1) Sentinel survivor — NOT a NULL probe. Migration 20260604_0600 DROPped
# the '1900-01-01' DEFAULT and the column is NOT NULL (no default), so a NULL
# cannot occur; the rot class is a sentinel VALUE that survived the drop or
# was written by a non-conforming loader. No sentinel may survive (A6).
_SENTINEL_LIFETIME_START_SQL = """
    SELECT count(*) FROM platform.ticker_classifications
    WHERE lifetime_start = DATE '1900-01-01'
"""

# (2) Pre-FPFD look-ahead — lifetime_start is anchored at the SEC FPFD, so a
# classification whose start predates its OWN first_public_filing_date
# (where FPFD is known) is the pre-rebuild contamination the rebuild cures.
# FPFD lives on ticker_classifications (verified live: issuers has no FPFD
# column), so this is a single-table self-check — no cik-join needed.
_LIFETIME_START_BEFORE_FPFD_SQL = """
    SELECT count(*) FROM platform.ticker_classifications tc
    WHERE tc.first_public_filing_date IS NOT NULL
      AND tc.lifetime_start < tc.first_public_filing_date
"""

# (3) ticker_history cross-classification overlap — two DISTINCT rows for the
# same TICKER whose half-open [valid_from, valid_to) windows overlap. The DB
# EXCLUDE (migration 20260524_0100) keys on classification_id WITH = so it
# guards ONLY same-classification overlap; cross-classification same-ticker
# overlap (G3 reuse) is NOT a DB invariant and MUST be caught here. Equal
# boundary (contiguous handoff) is NOT an overlap under the '[)' semantics.
_TICKER_HISTORY_OVERLAP_SQL = """
    SELECT count(*) FROM platform.ticker_history th1
    JOIN platform.ticker_history th2
      ON th1.ticker = th2.ticker
     AND th1.classification_id <> th2.classification_id
     AND daterange(th1.valid_from, COALESCE(th1.valid_to, 'infinity'::date), '[)')
       && daterange(th2.valid_from, COALESCE(th2.valid_to, 'infinity'::date), '[)')
"""

# (4) issuer_history overlap — issuer_history has NO EXCLUDE constraint
# (verified live: only PK + FK), so overlapping legal-name windows for the
# same issuer_id are guarded ONLY here. Mirrors the ticker_history probe,
# keyed on issuer_id.
_ISSUER_HISTORY_OVERLAP_SQL = """
    SELECT count(*) FROM platform.issuer_history ih1
    JOIN platform.issuer_history ih2
      ON ih1.issuer_id = ih2.issuer_id
     AND ih1.valid_from <> ih2.valid_from
     AND daterange(ih1.valid_from, COALESCE(ih1.valid_to, 'infinity'::date), '[)')
       && daterange(ih2.valid_from, COALESCE(ih2.valid_to, 'infinity'::date), '[)')
"""

# (5) Every cik-bearing STOCK/REIT classification must have an issuers row.
# The issuer satellite (CIK→issuer, FPFD, SCD-2) is an OPERATING-EQUITY model
# only (spec-delta 2026-06-05 decisions 1+7): ETFs/funds/SPACs/ADRs may carry
# a CIK as a provenance attribute (often a fund-trust CIK) but are NOT
# operating issuers, so they legitimately have no issuers row. Without the
# ``asset_class IN ('stock','reit')`` guard this probe reports every
# CIK-backed ETF (~2,632) as a false orphan and blocks a consistent substrate.
# The cik-join is zfill-10-normalized on BOTH sides: the FMP-fallback writer
# (scripts/ops.py, cik_source = 'fmp') may land an UNPADDED tc.cik, so a raw
# text compare would report a false orphan of a CONSISTENT substrate.
_CLASSIFICATION_WITHOUT_ISSUER_SQL = """
    SELECT count(*) FROM platform.ticker_classifications tc
    WHERE tc.cik IS NOT NULL
      AND tc.asset_class IN ('stock', 'reit')
      AND NOT EXISTS (
        SELECT 1 FROM platform.issuers AS iss_exists WHERE
          lpad(regexp_replace(iss_exists.cik, '[^0-9]', '', 'g'), 10, '0')
        = lpad(regexp_replace(tc.cik,         '[^0-9]', '', 'g'), 10, '0')
      )
"""

# (6) Every cik-bearing STOCK/REIT classification must have an
# issuer_securities link. Same operating-equity-only guard as probe 5.
_CLASSIFICATION_WITHOUT_ISSUER_SECURITIES_SQL = """
    SELECT count(*) FROM platform.ticker_classifications tc
    WHERE tc.cik IS NOT NULL
      AND tc.asset_class IN ('stock', 'reit')
      AND NOT EXISTS (
        SELECT 1 FROM platform.issuer_securities AS isec_link WHERE
          isec_link.classification_id = tc.id
      )
"""

# (7) Orphan classification_id — a ticker_history / issuer_securities row
# whose classification_id has no ticker_classifications row (the FK is not
# yet enforced at the DB level for ticker_history).
_ORPHAN_CLASSIFICATION_IN_TICKER_HISTORY_SQL = """
    SELECT count(*) FROM platform.ticker_history th
    WHERE NOT EXISTS (
      SELECT 1 FROM platform.ticker_classifications tc
      WHERE tc.id = th.classification_id
    )
"""
_ORPHAN_CLASSIFICATION_IN_ISSUER_SECURITIES_SQL = """
    SELECT count(*) FROM platform.issuer_securities isec
    WHERE NOT EXISTS (
      SELECT 1 FROM platform.ticker_classifications tc
      WHERE tc.id = isec.classification_id
    )
"""

# (8) Orphan issuer_id — a link / history row whose issuer_id has no issuers
# row.
_ORPHAN_ISSUER_SECURITIES_SQL = """
    SELECT count(*) FROM platform.issuer_securities es
    WHERE NOT EXISTS (
      SELECT 1 FROM platform.issuers i WHERE i.issuer_id = es.issuer_id
    )
"""
_ORPHAN_ISSUER_HISTORY_SQL = """
    SELECT count(*) FROM platform.issuer_history ih_orphan
    WHERE NOT EXISTS (
      SELECT 1 FROM platform.issuers i WHERE i.issuer_id = ih_orphan.issuer_id
    )
"""

# (9) Every etf/etn classification must have an etf_attributes satellite row.
# The ETF satellite (migration 20260607_0100, physical-entity separation) is a
# 1:1 attribute table for asset_class IN ('etf','etn'). A missing satellite row
# is seed-incompleteness — the spine still carries the etf_* columns in
# transition mode, but the satellite is the future home and must be complete.
_ETF_WITHOUT_ATTRIBUTES_SQL = """
    SELECT count(*) FROM platform.ticker_classifications tc
    WHERE tc.asset_class IN ('etf', 'etn')
      AND NOT EXISTS (
        SELECT 1 FROM platform.etf_attributes ea
        WHERE ea.classification_id = tc.id
      )
"""

# (10) The etf_attributes satellite must hold ONLY ETF/ETN attributes — a row
# pointing at a stock/spac/fund/adr/reit classification is a mis-scoped write.
_ETF_ATTRIBUTES_NON_ETF_SQL = """
    SELECT count(*) FROM platform.etf_attributes ea
    JOIN platform.ticker_classifications tc ON tc.id = ea.classification_id
    WHERE tc.asset_class NOT IN ('etf', 'etn')
"""

_PROBES: tuple[tuple[str, str], ...] = (
    ("sentinel_lifetime_start", _SENTINEL_LIFETIME_START_SQL),
    ("lifetime_start_before_fpfd", _LIFETIME_START_BEFORE_FPFD_SQL),
    ("ticker_history_overlaps", _TICKER_HISTORY_OVERLAP_SQL),
    ("issuer_history_overlaps", _ISSUER_HISTORY_OVERLAP_SQL),
    ("cik_classifications_without_issuer", _CLASSIFICATION_WITHOUT_ISSUER_SQL),
    (
        "cik_classifications_without_issuer_securities",
        _CLASSIFICATION_WITHOUT_ISSUER_SECURITIES_SQL,
    ),
    (
        "orphan_classification_in_ticker_history",
        _ORPHAN_CLASSIFICATION_IN_TICKER_HISTORY_SQL,
    ),
    (
        "orphan_classification_in_issuer_securities",
        _ORPHAN_CLASSIFICATION_IN_ISSUER_SECURITIES_SQL,
    ),
    ("orphan_issuer_id_in_securities", _ORPHAN_ISSUER_SECURITIES_SQL),
    ("orphan_issuer_id_in_history", _ORPHAN_ISSUER_HISTORY_SQL),
    ("etf_without_attributes", _ETF_WITHOUT_ATTRIBUTES_SQL),
    ("etf_attributes_non_etf", _ETF_ATTRIBUTES_NON_ETF_SQL),
)


async def evaluate_identity_gate(
    pool: _Fetchval, *, raise_on_fail: bool = False
) -> IdentityGateResult:
    """Run the post-build substrate-consistency probes.

    Read-only. Returns ``IdentityGateResult`` with the non-zero probe
    counts in ``violations`` (empty ⇒ clean). When ``raise_on_fail`` is
    True (the coordinator's BLOCKING mode), a non-empty ``violations``
    raises ``RuntimeError`` — the child loads must NOT proceed against an
    inconsistent identity substrate.
    """
    violations: dict[str, int] = {}
    for key, sql in _PROBES:
        count = await pool.fetchval(sql)
        n = int(count or 0)
        if n > 0:
            violations[key] = n

    passed = not violations
    logger.info(
        "identity_gate.evaluated",
        passed=passed,
        violations=violations,
    )
    result = IdentityGateResult(passed=passed, violations=violations)
    if raise_on_fail and not passed:
        raise RuntimeError(
            "identity gate FAILED — the identity substrate is internally "
            f"inconsistent: {violations}. Child loads must NOT proceed "
            "(Phase-1.4 blocking gate). Re-run the identity build; do not "
            "patch the substrate (identity-path rule)."
        )
    return result


__all__ = [
    "IdentityGateResult",
    "evaluate_identity_gate",
]
