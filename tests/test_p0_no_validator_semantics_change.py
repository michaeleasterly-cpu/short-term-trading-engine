"""P0 → P1 transition guard for the validator rewrite.

This sentinel began life as the P0 "no semantics change" canary —
pinning the byte-frozen SQL + constants of ``fundamentals_quarterly_completeness``
during the foundation patch. **P1 deliberately broke it.** The hash
+ constant values are now updated to the **P1 cadence-routed**
versions; the new sentinel guards against UNINTENTIONAL drift on top
of the P1 rewrite (the next semantic change must update this
sentinel explicitly, which forces a deliberate decision).

P1 ships the cadence-routed rewrite. See the module docstring on
``fundamentals_quarterly_completeness.py`` for the full design.
"""
from __future__ import annotations

import hashlib

from tpcore.quality.validation.checks import (
    fundamentals_quarterly_completeness as fqc,
)


def test_check_name_unchanged() -> None:
    assert fqc.CHECK_NAME == "fundamentals_quarterly_completeness"


def test_max_quarterly_gap_days_unchanged() -> None:
    """Quarterly gap constant survives P1 unchanged — 92 + 8 slack."""
    assert fqc.MAX_QUARTERLY_GAP_DAYS == 100


def test_max_annual_gap_days_p1() -> None:
    """P1 new constant: 365 + ~85 days slack for late 20-F filers
    (4-month deadline + tolerance) without false-firing a true skip
    (which would be ~730 days = two consecutive FY ends)."""
    assert fqc.MAX_ANNUAL_GAP_DAYS == 450


def test_live_within_days_routed_by_cadence_p1() -> None:
    """P1 replaces the single 120-day liveness gate with per-cadence
    gates. The pre-P1 single window silently darkened every annual
    filer; routing now matches cadence."""
    assert fqc.LIVE_WITHIN_DAYS_QUARTERLY == 120
    assert fqc.LIVE_WITHIN_DAYS_ANNUAL == 540


def test_metadata_coverage_threshold_p1() -> None:
    """P1 metadata-coverage structural sentinel — fires when > 25% of
    the active universe lacks sec_document_type_primary so a P0
    backfill regression cannot silently pass."""
    assert fqc.METADATA_COVERAGE_FAIL_THRESHOLD == 0.25


def test_tradeable_tier_max_unchanged() -> None:
    assert fqc.TRADEABLE_TIER_MAX == 2


def test_max_reported_unchanged() -> None:
    assert fqc.MAX_REPORTED == 25


def test_repair_lookback_buffer_unchanged() -> None:
    assert fqc.REPAIR_LOOKBACK_BUFFER_DAYS == 14


def test_filing_dates_sql_pinned_to_p1_shape() -> None:
    """The P1 SQL joins ``sec_document_type_primary`` and removes the
    ``asset_class = 'stock'`` predicate. Pinning the hash of the new
    SQL string is the canary against unintended drift atop the
    cadence-routed rewrite.

    If this hash regresses to the P0 value, P1 routing has been
    reverted by accident — block the change loudly here."""
    sha = hashlib.sha256(
        fqc._FILING_DATES_SQL.encode("utf-8"),
    ).hexdigest()
    assert sha == (
        "bbfa79597f63b0a467a0fa170a478b96893f271994925f932a05a0569ee1c2eb"
    ), (
        "fundamentals_quarterly_completeness._FILING_DATES_SQL changed "
        "from the P1 cadence-routed shape. If this is a deliberate next-"
        "phase rewrite, update the pinned hash. If it's a revert, "
        "restore the P1 routing (the SQL must SELECT "
        "sec_document_type_primary from ticker_classifications)."
    )
