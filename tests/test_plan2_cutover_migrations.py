"""Static sentinels for the Plan 2 clean-schema cutover migrations (no live DB).

Pins the revision chain 20260604_0200 -> 0300 -> 0500 -> 0600 and the exact DDL
each migration must carry, so a later edit cannot silently re-shape the
destructive cutover. These are file-text assertions only — the migrations are
applied live by the coordinator under the Task-7 gated sequence, never by CI.

NOTE (Plan 2 Phase 0): migration 0400 (count_snapshot→VIEW) was DROPPED —
``earnings_events_count_snapshot`` is a stateful monotone baseline, not a cache,
so it STAYS a mutable table and 0500 re-chains its down_revision to 0300.
"""
from __future__ import annotations

from pathlib import Path

_VERSIONS = Path("platform/migrations/versions")

DROP_MIG = _VERSIONS / "20260604_0300_drop_dead_and_folded_tables.py"
DQL_MIG = _VERSIONS / "20260604_0500_data_quality_log_redesign.py"
TIGHTEN_MIG = _VERSIONS / "20260604_0600_tighten_identity_fundamentals_schema.py"

DROPPED = [
    "tradier_options_chains",
    "options_max_pain",
    "ticker_lifecycle_events",
    "fundamentals_period_source_evidence",
    "parity_drift_log",
    "forensics_triggers",
    "ingestion_metrics",
]
# These must NOT be dropped by the cutover (KEPT standalone / deferred).
KEPT = ["split_pre_image_log", "ingest_quarantine", "failed_alpha_ledger", "ingest_manifest"]


# ── Task 3: DROP-set migration ────────────────────────────────────────────────


def test_drop_migration_pins_and_drops_only_the_dead_set() -> None:
    assert DROP_MIG.exists(), f"missing {DROP_MIG}"
    src = DROP_MIG.read_text()
    assert 'revision = "20260604_0300"' in src
    assert 'down_revision = "20260604_0200"' in src  # down_revision = Plan 1 head
    for t in DROPPED:
        assert f"DROP TABLE IF EXISTS platform.{t}" in src, f"{t} not dropped"
    for t in KEPT:
        assert f"DROP TABLE IF EXISTS platform.{t}" not in src, (
            f"{t} must NOT be dropped (kept/deferred)"
        )
    # options_max_pain's classification_id trigger + backing function are dropped.
    assert "tg_options_max_pain_classification_id" in src  # real trigger name
    assert "tg_set_classification_id_options_max_pain" in src  # real backing fn name


def test_drop_migration_downgrade_is_forward_only() -> None:
    src = DROP_MIG.read_text()
    assert "raise NotImplementedError" in src


# ── Task 4 REMOVED (Plan 2 Phase 0) ──────────────────────────────────────────
# Migration 0400 (count_snapshot→VIEW) was dropped: earnings_events_count_snapshot
# is a stateful monotone baseline (earnings_events_monotone SELECT … FOR UPDATE +
# upsert), NOT a cache — a VIEW always equals the live count and would defeat the
# monotone invariant. The table STAYS mutable; no migration is needed for it.


def test_count_snapshot_view_migration_is_gone() -> None:
    """The 0400 view-demotion migration must NOT exist (it was dropped)."""
    assert not (_VERSIONS / "20260604_0400_count_snapshot_to_view.py").exists()


# ── Task 5: data_quality_log redesign ─────────────────────────────────────────


def test_dql_redesign_shape() -> None:
    assert DQL_MIG.exists(), f"missing {DQL_MIG}"
    src = DQL_MIG.read_text()
    assert 'revision = "20260604_0500"' in src
    assert 'down_revision = "20260604_0300"' in src  # re-chained: 0400 dropped
    assert "DROP TABLE IF EXISTS platform.data_quality_log CASCADE" in src
    # uuid PK
    assert "id           uuid PRIMARY KEY DEFAULT gen_random_uuid()" in src
    # kind discriminator + CHECK over the enum
    assert "kind         text NOT NULL CHECK (kind IN (" in src
    for k in (
        "validation",
        "confirmed_data_gap_evidence",
        "parity_drift",
        "forensics_trigger",
        "backtest_credibility",
    ):
        # The CHECK enum is built at runtime from the KINDS tuple — match the
        # kind name in either quote style (single in emitted SQL, double in the
        # source tuple).
        assert f"'{k}'" in src or f'"{k}"' in src, f"kind {k} missing from CHECK enum"
    # typed-cols-validation-only CHECK
    assert "CONSTRAINT dql_typed_cols_validation_only CHECK (" in src
    assert "kind = 'validation'" in src
    # notes is jsonb
    assert "notes        jsonb" in src
    # the 4 partial / gin indexes
    assert "ix_dql_validation" in src
    assert "ix_dql_parity_drift" in src
    assert "ix_dql_forensics" in src
    assert "ix_dql_notes_gin" in src
    assert "USING gin (notes)" in src


# ── Task 8: schema-tighten ────────────────────────────────────────────────────


def test_tighten_migration_ddl() -> None:
    assert TIGHTEN_MIG.exists(), f"missing {TIGHTEN_MIG}"
    src = TIGHTEN_MIG.read_text()
    assert 'revision = "20260604_0600"' in src
    assert 'down_revision = "20260604_0500"' in src
    # lifetime_start drops its default
    assert "ALTER COLUMN lifetime_start DROP DEFAULT" in src
    # fq 3-part natural PK using the REAL constraint names confirmed live
    assert "ALTER COLUMN period_end_date SET NOT NULL" in src
    assert "ALTER COLUMN filing_date SET NOT NULL" in src
    assert "DROP CONSTRAINT IF EXISTS fundamentals_quarterly_pkey" in src
    assert "DROP CONSTRAINT IF EXISTS uq_fundamentals_ticker_filing" in src
    assert "ADD PRIMARY KEY (ticker, period_end_date, filing_date)" in src


def test_tighten_migration_is_empty_table_self_protecting() -> None:
    """0600 must refuse to run on a populated fundamentals_quarterly (the
    3-part PK would fail mid-apply on legacy data). Guards the correct
    sequence: upgrade 0500 -> wipe -> upgrade head."""
    src = TIGHTEN_MIG.read_text()
    assert "SELECT count(*) FROM platform.fundamentals_quarterly" in src
    assert "raise RuntimeError" in src
    assert "op.get_bind()" in src


# ── Cross-cutting: the revision chain is a single line 0200..0600 ─────────────


def test_revision_chain_is_linear_and_unbroken() -> None:
    # 0400 was dropped (Plan 2 Phase 0); the chain is 0200→0300→0500→0600.
    chain = [
        ("20260604_0300", "20260604_0200", DROP_MIG),
        ("20260604_0500", "20260604_0300", DQL_MIG),
        ("20260604_0600", "20260604_0500", TIGHTEN_MIG),
    ]
    for rev, down, path in chain:
        src = path.read_text()
        assert f'revision = "{rev}"' in src, f"{path.name} revision mismatch"
        assert f'down_revision = "{down}"' in src, f"{path.name} down_revision mismatch"
