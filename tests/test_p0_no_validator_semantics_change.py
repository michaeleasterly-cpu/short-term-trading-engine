"""P0-foundation guard — the validator's PASS/FAIL behavior MUST NOT
change in this patch (operator hard rule).

This sentinel pins the byte-identical constants + SQL on
``fundamentals_quarterly_completeness`` so the next person to "fix"
the validator alongside foundation work breaks loudly here.

The future five-state rewrite (P1) is expected to change these — at
which point the asserted values in this test will be updated as the
deliberate semantic change. Today (P0) they MUST stay frozen.
"""
from __future__ import annotations

import hashlib

from tpcore.quality.validation.checks import (
    fundamentals_quarterly_completeness as fqc,
)


def test_check_name_unchanged() -> None:
    assert fqc.CHECK_NAME == "fundamentals_quarterly_completeness"


def test_max_quarterly_gap_days_unchanged() -> None:
    assert fqc.MAX_QUARTERLY_GAP_DAYS == 100


def test_live_within_days_unchanged() -> None:
    assert fqc.LIVE_WITHIN_DAYS == 120


def test_tradeable_tier_max_unchanged() -> None:
    assert fqc.TRADEABLE_TIER_MAX == 2


def test_max_reported_unchanged() -> None:
    assert fqc.MAX_REPORTED == 25


def test_repair_lookback_buffer_unchanged() -> None:
    assert fqc.REPAIR_LOOKBACK_BUFFER_DAYS == 14


def test_filing_dates_sql_unchanged() -> None:
    """The SQL the check + healer share for filing-date enumeration is
    frozen in P0. The validator still reads ``tc.asset_class = 'stock'``
    today; the P1 rewrite is expected to replace this with an evidence-
    derived issuer-class predicate. Pinning the hash of the current SQL
    string is the sentinel."""
    sha = hashlib.sha256(
        fqc._FILING_DATES_SQL.encode("utf-8"),
    ).hexdigest()
    assert sha == (
        "736d761676bef0ae46c8e293a2f431238cf2e2e92605f1f8b998b4bf2e535469"
    ), (
        "fundamentals_quarterly_completeness._FILING_DATES_SQL changed "
        "— if this is the P1 rewrite, update the pinned hash. If this "
        "is P0 work, revert: the operator's hard rule forbids "
        "validator semantics change in the foundation patch."
    )
