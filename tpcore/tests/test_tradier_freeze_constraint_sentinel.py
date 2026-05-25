"""Sentinel: the P0_5 ``prices_daily_no_new_tradier`` CHECK constraint
migration must exist + contain the expected forward-only gate.

The constraint itself is enforced at the DB layer (Postgres rejects
new ``source='tradier'`` INSERTs). This test guards against accidental
removal / weakening of the migration file in the repo.
"""

from __future__ import annotations

from pathlib import Path

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "platform" / "migrations" / "versions"
    / "20260525_1100_freeze_tradier_source.py"
)


def test_tradier_freeze_migration_exists() -> None:
    assert _MIGRATION.is_file(), (
        "P0_5 tradier-freeze migration disappeared — "
        f"expected at {_MIGRATION}. Postgres-side constraint "
        "'prices_daily_no_new_tradier' may still be live, but the "
        "repo loses its rebuild capability + audit trail."
    )


def test_tradier_freeze_migration_contains_expected_sql() -> None:
    src = _MIGRATION.read_text()
    # Constraint name + the source != tradier predicate.
    assert "prices_daily_no_new_tradier" in src
    assert "source <> 'tradier'" in src
    # NOT VALID is load-bearing: without it the migration would scan
    # the 15M existing tradier rows and fail.
    assert "NOT VALID" in src
    # Target table — must be the production prices_daily, not some
    # staging clone.
    assert "platform.prices_daily" in src
    # Forward-only direction (down_revision points at the prior P3
    # staging migration — the chain).
    assert '"20260525_0900"' in src or '\'20260525_0900\'' in src
