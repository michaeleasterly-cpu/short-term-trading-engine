"""Migration shape sentinels for the ticker-reuse cleanup arc (PR #440 impl).

Pins:
* migration revision + down_revision
* archive table has all 20 mirror columns + 4 audit columns + 2 indexes
* quarantine table has all 20 mirror columns + 5 audit columns +
  the disposition CHECK constraint
* the disposition enum is exactly the 4 plan-allowed values

Stdlib only. No DB. No network. Parses the migration source as text.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_MIG = _REPO / "platform" / "migrations" / "versions" / (
    "20260602_0100_fundamentals_quarterly_archive_quarantine.py"
)


def _mig_text() -> str:
    assert _MIG.is_file(), f"missing {_MIG.relative_to(_REPO)}"
    text = _MIG.read_text(encoding="utf-8")
    assert text.strip(), "migration is empty"
    return text


def test_revision_metadata_pinned() -> None:
    text = _mig_text()
    assert 'revision: str = "20260602_0100"' in text
    assert 'down_revision: str | None = "20260601_0100"' in text


def test_creates_archive_table_with_mirror_and_audit_columns() -> None:
    text = _mig_text()
    assert '"fundamentals_quarterly_archive"' in text
    # Audit columns required by plan §4.1
    for c in (
        "archived_at", "disposition_reason",
        "decided_by_run_id", "evidence_summary",
    ):
        assert f'"{c}"' in text, (
            f"archive table must carry audit column {c!r}"
        )


def test_creates_quarantine_table_with_disposition_enum() -> None:
    text = _mig_text()
    assert '"fundamentals_quarterly_quarantine"' in text
    # CHECK constraint must allow exactly the 4 plan-enumerated values.
    for v in (
        "ambiguous_predecessor_unknown",
        "corp_history_substrate_sparse",
        "cik_null",
        "operator_review_pending",
    ):
        assert f"'{v}'" in text, (
            f"quarantine CHECK constraint must include {v!r}"
        )
    assert "ck_fq_quarantine_disposition" in text, (
        "CHECK constraint name must be ck_fq_quarantine_disposition"
    )


def test_mirror_columns_match_fundamentals_quarterly_shape() -> None:
    text = _mig_text()
    # The 20-column mirror per plan §4.
    expected = [
        "ticker", "filing_date", "period_end_date", "period_label",
        "net_income", "fcf", "operating_cash_flow", "capex", "revenue",
        "total_assets", "total_liabilities", "current_assets",
        "current_liabilities", "receivables", "cash_and_equivalents",
        "shares_outstanding", "recorded_at", "pb", "de",
        "classification_id",
    ]
    for col in expected:
        assert f'"{col}"' in text, (
            f"sidecar tables must mirror fundamentals_quarterly "
            f"column {col!r}"
        )


def test_archive_indexes_present() -> None:
    text = _mig_text()
    assert "ix_fq_archive_run" in text
    assert "ix_fq_archive_ticker" in text
    # Run-id index must be ordered by archived_at for time-series scan.
    assert re.search(
        r'"ix_fq_archive_run".*?\["decided_by_run_id", "archived_at"\]',
        text, re.DOTALL,
    ), "archive run index must be on (decided_by_run_id, archived_at)"


def test_quarantine_indexes_present() -> None:
    text = _mig_text()
    assert "ix_fq_quarantine_run" in text
    assert "ix_fq_quarantine_ticker" in text


def test_quarantine_carries_promoted_back_at_for_roundtrip() -> None:
    text = _mig_text()
    assert '"promoted_back_at"' in text, (
        "quarantine table must carry promoted_back_at so a future "
        "operator restore can record the round-trip"
    )


def test_downgrade_drops_both_tables() -> None:
    text = _mig_text()
    assert 'op.drop_table(\n        "fundamentals_quarterly_quarantine"' in text
    assert 'op.drop_table(\n        "fundamentals_quarterly_archive"' in text


def test_main_fundamentals_quarterly_not_touched() -> None:
    text = _mig_text()
    # Upgrade must not contain CREATE/ALTER on the main fq table.
    for forbidden in (
        'op.create_table(\n        "fundamentals_quarterly"',
        'op.alter_column("fundamentals_quarterly"',
        'op.drop_table("fundamentals_quarterly")',
    ):
        assert forbidden not in text, (
            f"migration must not touch main fundamentals_quarterly "
            f"({forbidden!r} found)"
        )


def test_original_id_column_present_for_roundtrip() -> None:
    text = _mig_text()
    assert '"original_id"' in text, (
        "sidecars must record the source row's id so the rollback "
        "query can target the exact row identity"
    )
