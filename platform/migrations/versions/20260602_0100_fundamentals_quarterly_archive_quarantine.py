"""fundamentals_quarterly archive + quarantine sidecar tables

Revision ID: 20260602_0100
Revises: 20260601_0100
Create Date: 2026-06-02

Per the ticker-reuse cleanup plan PR #440 §4. Adds two sidecar tables
that hold the row-level disposition of pre-FPFD `fundamentals_quarterly`
rows during the ticker-reuse cleanup arc:

* `platform.fundamentals_quarterly_archive` — 1-to-1 mirror of
  `fundamentals_quarterly` + 4 audit columns. The destination for
  high-confidence ticker-reuse rows that are moved out of the main
  table via the archive-before-delete transaction in the
  `cleanup_ticker_reuse_fundamentals` stage.

* `platform.fundamentals_quarterly_quarantine` — same 1-to-1 mirror
  + `disposition` CHECK constraint + `quarantined_at` +
  `promoted_back_at`. The destination for ambiguous-evidence rows
  pending operator triage.

The main `fundamentals_quarterly` table is NOT touched — validator,
engines, dashboards, and backtest continue to read it AS-IS.

Both tables are idempotent rollback targets (`promoted_back_at` records
the round-trip). Per plan §9, the rollback path is a SELECT-INTO from
the sidecar back to the main table, scoped by `decided_by_run_id`.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260602_0100"
down_revision: str | None = "20260601_0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ─── shared mirror-of-fundamentals_quarterly column list ─────────────


def _mirror_columns() -> list[sa.Column]:
    """The 20 columns of `platform.fundamentals_quarterly` plus a
    nullable wrapper `original_id` for round-trip restoration.

    `original_id` carries the source row's `id` so a future
    restore-from-sidecar can target the exact row identity. We do NOT
    make it the sidecar's PRIMARY KEY because a single source row could
    be archived → restored → re-archived under different run_ids
    (rare but valid)."""
    return [
        sa.Column("original_id", sa.BigInteger),
        sa.Column("ticker", sa.Text, nullable=False),
        sa.Column("filing_date", sa.Date, nullable=False),
        sa.Column("period_end_date", sa.Date, nullable=False),
        sa.Column("period_label", sa.Text),
        sa.Column("net_income", sa.Numeric(20, 4)),
        sa.Column("fcf", sa.Numeric(20, 4)),
        sa.Column("operating_cash_flow", sa.Numeric(20, 4)),
        sa.Column("capex", sa.Numeric(20, 4)),
        sa.Column("revenue", sa.Numeric(20, 4)),
        sa.Column("total_assets", sa.Numeric(20, 4)),
        sa.Column("total_liabilities", sa.Numeric(20, 4)),
        sa.Column("current_assets", sa.Numeric(20, 4)),
        sa.Column("current_liabilities", sa.Numeric(20, 4)),
        sa.Column("receivables", sa.Numeric(20, 4)),
        sa.Column("cash_and_equivalents", sa.Numeric(20, 4)),
        sa.Column("shares_outstanding", sa.Numeric(20, 4)),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("pb", sa.Numeric(20, 6)),
        sa.Column("de", sa.Numeric(20, 6)),
        sa.Column("classification_id", sa.BigInteger),
    ]


def upgrade() -> None:
    # ─── archive table ──────────────────────────────────────────────
    op.create_table(
        "fundamentals_quarterly_archive",
        sa.Column(
            "id", sa.BigInteger, primary_key=True, autoincrement=True,
        ),
        *_mirror_columns(),
        # Audit columns — operator's plan PR #440 §4.1.
        sa.Column(
            "archived_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("disposition_reason", sa.Text, nullable=False),
        sa.Column(
            "decided_by_run_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("evidence_summary", sa.Text, nullable=False),
        schema="platform",
    )
    op.create_index(
        "ix_fq_archive_run",
        "fundamentals_quarterly_archive",
        ["decided_by_run_id", "archived_at"],
        schema="platform",
    )
    op.create_index(
        "ix_fq_archive_ticker",
        "fundamentals_quarterly_archive",
        ["ticker", "archived_at"],
        schema="platform",
    )

    # ─── quarantine table ───────────────────────────────────────────
    op.create_table(
        "fundamentals_quarterly_quarantine",
        sa.Column(
            "id", sa.BigInteger, primary_key=True, autoincrement=True,
        ),
        *_mirror_columns(),
        sa.Column(
            "quarantined_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "disposition",
            sa.Text,
            nullable=False,
        ),
        sa.Column(
            "decided_by_run_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("evidence_summary", sa.Text, nullable=False),
        sa.Column("promoted_back_at", sa.TIMESTAMP(timezone=True)),
        sa.CheckConstraint(
            "disposition IN ("
            "'ambiguous_predecessor_unknown', "
            "'corp_history_substrate_sparse', "
            "'cik_null', "
            "'operator_review_pending'"
            ")",
            name="ck_fq_quarantine_disposition",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_fq_quarantine_run",
        "fundamentals_quarterly_quarantine",
        ["decided_by_run_id", "quarantined_at"],
        schema="platform",
    )
    op.create_index(
        "ix_fq_quarantine_ticker",
        "fundamentals_quarterly_quarantine",
        ["ticker", "quarantined_at"],
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fq_quarantine_ticker",
        table_name="fundamentals_quarterly_quarantine",
        schema="platform",
    )
    op.drop_index(
        "ix_fq_quarantine_run",
        table_name="fundamentals_quarterly_quarantine",
        schema="platform",
    )
    op.drop_table(
        "fundamentals_quarterly_quarantine",
        schema="platform",
    )
    op.drop_index(
        "ix_fq_archive_ticker",
        table_name="fundamentals_quarterly_archive",
        schema="platform",
    )
    op.drop_index(
        "ix_fq_archive_run",
        table_name="fundamentals_quarterly_archive",
        schema="platform",
    )
    op.drop_table(
        "fundamentals_quarterly_archive",
        schema="platform",
    )
