"""Sentinel: the P0_5 (operator-corrected) ``prices_daily_no_new_alpaca``
CHECK constraint migration must exist + contain the expected
forward-only gate.

The original "P0_5 tradier freeze" framing was reversed (memory
``feedback_no_alpaca_for_daily_prices_backfill`` 2026-05-25):

    FMP primary > Tradier secondary (acceptable) > NEVER Alpaca.

Migration ``20260525_1200`` adds the CHECK forbidding ``source =
'alpaca'`` and REPLACEs ``_source_priority`` to demote alpaca to 0
(from peer-of-sip=3). This sentinel guards against accidental
removal in the repo.
"""

from __future__ import annotations

from pathlib import Path

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "platform" / "migrations" / "versions"
    / "20260525_1200_freeze_alpaca_source_and_repriority.py"
)


def test_alpaca_freeze_migration_exists() -> None:
    assert _MIGRATION.is_file(), (
        "P0_5 alpaca-freeze migration disappeared — "
        f"expected at {_MIGRATION}. The Postgres-side constraint "
        "'prices_daily_no_new_alpaca' may still be live, but the "
        "repo loses its rebuild capability + audit trail."
    )


def test_alpaca_freeze_migration_contains_expected_sql() -> None:
    src = _MIGRATION.read_text()
    # CHECK constraint name + the predicate.
    assert "prices_daily_no_new_alpaca" in src
    assert "source <> 'alpaca'" in src
    # NOT VALID is load-bearing — the 2.7M existing alpaca rows
    # mustn't be scanned.
    assert "NOT VALID" in src
    # Target the production table.
    assert "platform.prices_daily" in src
    # The corrected priority function: fmp=4 / tradier=3 / sip=2 /
    # iex=1 / alpaca=0. A regression that re-promotes alpaca would
    # be caught by these substring asserts.
    assert "WHEN 'fmp'     THEN 4::smallint" in src
    assert "WHEN 'tradier' THEN 3::smallint" in src
    assert "WHEN 'sip'     THEN 2::smallint" in src
    assert "WHEN 'iex'     THEN 1::smallint" in src
    assert "WHEN 'alpaca'  THEN 0::smallint" in src
    # Forward-only chain (down_revision points at the prior P3
    # staging migration; the earlier — invalidated — tradier-freeze
    # at 20260525_1100 is gone).
    assert '"20260525_0900"' in src or "'20260525_0900'" in src
