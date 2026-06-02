"""Migration sentinel for the `excluded_confirmed_data_gap`
validator-semantics arc — spec PR #450 + plan PR #451.

Verifies that `platform/migrations/versions/20260602_0200_
fundamentals_period_source_evidence.py` declares the load-bearing
schema pieces:

  * `revision = "20260602_0200"` + `down_revision = "20260602_0100"`
  * Creates `platform.fundamentals_period_source_evidence`
  * PK = `(ticker, period_end_date, source)`
  * `outcome` CHECK enum carries all 4 values
  * `source` CHECK enum carries all 3 values
  * `(ticker, period_end_date)` index present
  * `updated_at` trigger function + trigger present
  * Downgrade path: drops trigger → function → index → table

Static parse of the migration source; no live DB required.
"""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_MIGRATION = (
    _REPO / "platform" / "migrations" / "versions"
    / "20260602_0200_fundamentals_period_source_evidence.py"
)


def _src() -> str:
    assert _MIGRATION.is_file(), f"missing migration: {_MIGRATION}"
    return _MIGRATION.read_text(encoding="utf-8")


def test_migration_revision_pinned() -> None:
    src = _src()
    assert 'revision: str = "20260602_0200"' in src
    assert 'down_revision: str | None = "20260602_0100"' in src


def test_migration_creates_evidence_table() -> None:
    src = _src()
    assert (
        "CREATE TABLE IF NOT EXISTS "
        "platform.fundamentals_period_source_evidence"
    ) in src


def test_migration_declares_primary_key() -> None:
    """PK triple `(ticker, period_end_date, source)` per spec §5.1."""
    src = _src()
    assert "fundamentals_period_source_evidence_pk" in src
    assert "PRIMARY KEY (ticker, period_end_date, source)" in src


def test_migration_declares_outcome_check_enum() -> None:
    src = _src()
    # Constraint name + every enum value.
    assert "fundamentals_period_source_evidence_outcome_check" in src
    for value in ("yielded", "empty", "extract_none", "fetch_failure"):
        assert f"'{value}'" in src, (
            f"outcome enum missing {value!r}"
        )


def test_migration_declares_source_check_enum() -> None:
    src = _src()
    assert "fundamentals_period_source_evidence_source_check" in src
    for value in ("fmp_historical", "fmp_refresh", "sec_companyfacts"):
        assert f"'{value}'" in src, f"source enum missing {value!r}"


def test_migration_declares_ticker_period_index() -> None:
    src = _src()
    assert (
        "fundamentals_period_source_evidence_ticker_period_idx"
    ) in src
    assert "(ticker, period_end_date)" in src


def test_migration_declares_updated_at_trigger() -> None:
    src = _src()
    assert (
        "fundamentals_period_source_evidence_touch_updated_at"
    ) in src
    assert (
        "fundamentals_period_source_evidence_updated_at_trg"
    ) in src
    assert "BEFORE UPDATE" in src
    assert "EXECUTE FUNCTION" in src


def test_migration_downgrade_drops_trigger_function_index_table() -> None:
    src = _src()
    assert (
        "DROP TRIGGER IF EXISTS "
        "fundamentals_period_source_evidence_updated_at_trg"
    ) in src
    # Function drop carries trailing ``()`` per the Postgres
    # ``DROP FUNCTION`` grammar.
    assert (
        "DROP FUNCTION IF EXISTS "
        "platform.fundamentals_period_source_evidence_touch_updated_at()"
    ) in src
    assert (
        "DROP INDEX IF EXISTS "
        "platform.fundamentals_period_source_evidence_ticker_period_idx"
    ) in src
    assert (
        "DROP TABLE IF EXISTS "
        "platform.fundamentals_period_source_evidence"
    ) in src
