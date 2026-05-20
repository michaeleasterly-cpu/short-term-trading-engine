"""sec_insider_row_counts_snapshot — per-ticker row-count baseline for the
zero-tolerance monotone-non-decrease invariant on platform.sec_insider_transactions.

Form 4 filings are append-only historical events — a 2019 insider sale
does NOT unhappen. The monotone invariant
(``sec_insider_monotone``) therefore demands that for every ticker the
live ``COUNT(*)`` across runs is non-decreasing. To compare across runs
we need a durable per-ticker baseline; this table is it.

Shape:

* PRIMARY KEY (ticker) — one row per ticker, NOT a history table.
  Each run UPSERTs the current count after a successful compare; the
  next run gates against THAT.
* ``rowcount BIGINT`` — Form 4 transactions can run to hundreds of
  thousands of rows per ticker over a multi-year backfill window.
* ``snapshot_at TIMESTAMPTZ`` — debugging aid (when was this baseline
  last touched), NOT a comparison key.

The check itself runs the read + compare + UPSERT in a single
transaction so a crash mid-update can't poison the next cycle's
baseline.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260520_0000"
down_revision: str | None = "20260519_0000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sec_insider_row_counts_snapshot",
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("rowcount", sa.BigInteger(), nullable=False),
        sa.Column(
            "snapshot_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "ticker", name="sec_insider_row_counts_snapshot_pk"
        ),
        sa.CheckConstraint(
            "rowcount >= 0", name="sec_insider_row_counts_snapshot_nonneg_chk"
        ),
        schema="platform",
    )
    op.execute(
        "COMMENT ON TABLE platform.sec_insider_row_counts_snapshot IS "
        "'Per-ticker Form 4 transaction row-count baseline. Gates the "
        "zero-tolerance sec_insider_monotone invariant: every ticker''s "
        "live COUNT(*) must be >= the value here. Form 4 is append-only; "
        "any per-ticker decrease is vendor truncation / deletion event.'"
    )


def downgrade() -> None:
    op.drop_table(
        "sec_insider_row_counts_snapshot", schema="platform"
    )
