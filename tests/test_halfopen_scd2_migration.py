"""Static sentinels for the half-open SCD-2 predicate migration (no live DB)."""
from __future__ import annotations

from pathlib import Path

MIG = Path("platform/migrations/versions/20260604_0100_halfopen_scd2_predicate_14_triggers.py")

# The 14 tables whose trigger functions must be rewritten (options_max_pain excluded; aar_events included).
EXPECTED_TABLES = {
    "prices_daily", "fundamentals_quarterly", "earnings_events", "corporate_actions",
    "insider_transactions", "sec_material_events", "short_interest", "borrow_rates",
    "liquidity_tiers", "insider_sentiment", "social_sentiment", "spread_observations",
    "universe_candidates", "aar_events",
}


def _src() -> str:
    assert MIG.exists(), f"migration not found: {MIG}"
    return MIG.read_text()


def test_revision_and_down_revision_pinned() -> None:
    src = _src()
    assert 'revision = "20260604_0100"' in src or "revision: str = \"20260604_0100\"" in src
    assert "20260602_0200" in src  # down_revision pins to current HEAD


def test_uses_half_open_predicate_not_closed() -> None:
    src = _src()
    # half-open present, closed absent
    assert "as_of < valid_to" in src or "$2 < valid_to" in src or "< valid_to" in src
    assert "valid_to >= " not in src, "closed predicate `valid_to >= ...` must not survive in the rebuild migration"


def test_covers_all_14_tables_and_not_options_max_pain() -> None:
    src = _src()
    for t in EXPECTED_TABLES:
        assert t in src, f"trigger function for {t} missing from migration"
    assert "options_max_pain" not in src, "options_max_pain trigger is DROPPED, must not be (re)created here"


def test_short_interest_as_of_is_release_date() -> None:
    src = _src()
    assert "NEW.release_date" in src, "short_interest as-of must be release_date (invariant B7)"


FK_MIG = Path("platform/migrations/versions/20260604_0200_aar_events_classification_id_fk.py")


def test_aar_fk_migration_pins_and_adds_fk() -> None:
    assert FK_MIG.exists(), f"migration not found: {FK_MIG}"
    src = FK_MIG.read_text()
    assert "20260604_0100" in src                      # down_revision = the trigger migration
    assert "aar_events" in src and "classification_id" in src
    assert "REFERENCES platform.ticker_classifications" in src
    assert "ON UPDATE CASCADE" in src and "ON DELETE RESTRICT" in src
    assert "NOT VALID" in src and "VALIDATE CONSTRAINT" in src
