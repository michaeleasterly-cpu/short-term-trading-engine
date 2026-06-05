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

  1. **0 classifications with NULL ``lifetime_start``** — the no-sentinel
     A6 invariant (universe_build always sets it).
  2. **0 ``ticker_history`` overlaps** — the half-open windows for the same
     ticker must not overlap (the EXCLUDE constraint enforces this at write
     time, but a pre-existing dirty row would be surfaced here).
  3. **Every cik-bearing classification has an ``issuers`` row** (issuers_build
     covered every CIK).
  4. **Every cik-bearing classification has an ``issuer_securities`` link**
     (issuer_securities_build fanned every cik-bearing security out).
  5. **0 orphan ``issuer_id``** in ``issuer_securities`` / ``issuer_history``
     (every referenced issuer exists — the M:N + SCD-2 tables point only
     at real issuers).

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
_NULL_LIFETIME_START_SQL = """
    SELECT count(*) FROM platform.ticker_classifications
    WHERE lifetime_start IS NULL
"""

# A self-join overlap probe — two DISTINCT rows for the same ticker whose
# half-open [valid_from, valid_to) windows overlap. Equal boundary
# (contiguous handoff) is NOT an overlap under the '[)' semantics.
_TICKER_HISTORY_OVERLAP_SQL = """
    SELECT count(*) FROM platform.ticker_history th1
    JOIN platform.ticker_history th2
      ON th1.ticker = th2.ticker
     AND th1.classification_id <> th2.classification_id
     AND daterange(th1.valid_from, COALESCE(th1.valid_to, 'infinity'::date), '[)')
       && daterange(th2.valid_from, COALESCE(th2.valid_to, 'infinity'::date), '[)')
"""

# Every cik-bearing classification must have an issuers row (matched on the
# 'CIK'+zero-padded-10 issuer_id). NOT EXISTS keyed on cik.
_CLASSIFICATION_WITHOUT_ISSUER_SQL = """
    SELECT count(*) FROM platform.ticker_classifications tc
    WHERE tc.cik IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM platform.issuers AS iss WHERE iss.cik = tc.cik
      )
"""

# Every cik-bearing classification must have an issuer_securities link.
_CLASSIFICATION_WITHOUT_ISSUER_SECURITIES_SQL = """
    SELECT count(*) FROM platform.ticker_classifications tc
    WHERE tc.cik IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM platform.issuer_securities AS isec WHERE
          isec.classification_id = tc.id
      )
"""

# Orphan issuer_id — a link / history row whose issuer_id has no issuers row.
_ORPHAN_ISSUER_SECURITIES_SQL = """
    SELECT count(*) FROM platform.issuer_securities es
    WHERE NOT EXISTS (
      SELECT 1 FROM platform.issuers i WHERE i.issuer_id = es.issuer_id
    )
"""
_ORPHAN_ISSUER_HISTORY_SQL = """
    SELECT count(*) FROM platform.issuer_history ih
    WHERE NOT EXISTS (
      SELECT 1 FROM platform.issuers i WHERE i.issuer_id = ih.issuer_id
    )
"""

_PROBES: tuple[tuple[str, str], ...] = (
    ("null_lifetime_start", _NULL_LIFETIME_START_SQL),
    ("ticker_history_overlaps", _TICKER_HISTORY_OVERLAP_SQL),
    ("cik_classifications_without_issuer", _CLASSIFICATION_WITHOUT_ISSUER_SQL),
    (
        "cik_classifications_without_issuer_securities",
        _CLASSIFICATION_WITHOUT_ISSUER_SECURITIES_SQL,
    ),
    ("orphan_issuer_id_in_securities", _ORPHAN_ISSUER_SECURITIES_SQL),
    ("orphan_issuer_id_in_history", _ORPHAN_ISSUER_HISTORY_SQL),
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
