"""Schema sentinels for the data-foundation classification_id finalize.

Two Phase-C/D migrations (2026-06-08) close the child-data identity arc:

  * ``20260608_0400_ingest_excluded_pre_existence.py`` — the durable,
    evidence-backed pre-existence exclusion trail (the 6,733 predecessor /
    FMP-synthetic-artifact rows removed from the child tables, each with its
    SEC earliest-filing evidence).
  * ``20260608_0500_classification_id_not_null.py`` — SET NOT NULL on
    ``classification_id`` for the 7 formerly-nullable child tables (the final
    lock: every ticker-bearing row resolves to an entity window, enforced by
    the database, not merely a validator).

Static-source assertions (no live DB needed) — CI runs without an operator DSN.
These red on a deliberate migration mutation (wrong revision chain, a dropped
table from the NOT-NULL set, a missing downgrade, a CHECK-reason drift).
"""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_VERSIONS = _REPO / "platform" / "migrations" / "versions"
_TRAIL = _VERSIONS / "20260608_0400_ingest_excluded_pre_existence.py"
_NOTNULL = _VERSIONS / "20260608_0500_classification_id_not_null.py"

# The 7 tables whose classification_id is locked NOT NULL (insider_transactions
# + sec_material_events were ALREADY NOT NULL and are intentionally excluded).
_NOT_NULL_TABLES = (
    "prices_daily",
    "corporate_actions",
    "earnings_events",
    "fundamentals_quarterly",
    "sec_periodic_filings",
    "aar_events",
    "short_interest",
)

# The two reasons the evidence trail's CHECK constraint admits.
_REASONS = (
    "pre_existence_predecessor",
    "pre_existence_artifact_no_sec_entity",
)


def _trail() -> str:
    assert _TRAIL.is_file(), f"missing migration: {_TRAIL}"
    return _TRAIL.read_text(encoding="utf-8")


def _notnull() -> str:
    assert _NOTNULL.is_file(), f"missing migration: {_NOTNULL}"
    return _NOTNULL.read_text(encoding="utf-8")


def test_trail_revision_chain_pinned() -> None:
    src = _trail()
    assert 'revision = "20260608_0400"' in src
    assert 'down_revision = "20260608_0200"' in src


def test_trail_creates_evidence_table() -> None:
    src = _trail()
    assert (
        "CREATE TABLE IF NOT EXISTS platform.ingest_excluded_pre_existence" in src
    )


def test_trail_declares_evidence_columns() -> None:
    """The trail must record the (table, ticker, event_date) key + the SEC
    evidence (resolved_cik / resolved_cid / entity_lifetime_start /
    sec_earliest_filing) + the reason — the anti-fake-green proof."""
    src = _trail()
    for col in (
        "tbl",
        "ticker",
        "event_date",
        "resolved_cik",
        "resolved_cid",
        "entity_lifetime_start",
        "sec_earliest_filing",
        "reason",
    ):
        assert col in src, f"evidence trail missing column: {col}"


def test_trail_reason_check_enumerates_both_pre_existence_classes() -> None:
    src = _trail()
    assert "ingest_excluded_pre_existence_reason_ck" in src
    for reason in _REASONS:
        assert f"'{reason}'" in src, f"trail CHECK missing reason: {reason}"


def test_trail_downgrade_drops_table() -> None:
    src = _trail()
    assert "DROP TABLE IF EXISTS platform.ingest_excluded_pre_existence" in src


def test_notnull_revision_chain_pinned() -> None:
    src = _notnull()
    assert 'revision = "20260608_0500"' in src
    assert 'down_revision = "20260608_0400"' in src


def test_notnull_covers_exactly_the_seven_formerly_nullable_tables() -> None:
    src = _notnull()
    for tbl in _NOT_NULL_TABLES:
        assert f'"{tbl}"' in src, f"NOT-NULL migration missing table: {tbl}"
    # The two ALREADY-NOT-NULL tables must NOT be re-altered here.
    for already in ("insider_transactions", "sec_material_events"):
        assert f'"{already}"' not in src, (
            f"{already} was already NOT NULL — must not be in the SET NOT NULL set"
        )


def test_notnull_sets_not_null_and_downgrade_drops_it() -> None:
    src = _notnull()
    assert "ALTER COLUMN classification_id SET NOT NULL" in src
    assert "ALTER COLUMN classification_id DROP NOT NULL" in src
