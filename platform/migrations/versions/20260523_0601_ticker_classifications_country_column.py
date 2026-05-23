"""Phase 1.2 — add country char(2) on ticker_classifications.

Per v2 plan §3.2 and v2 spec §12. NULLABLE first (backfill from Alpaca
in the same PR's producer code); CHECK constraint and NOT NULL deferred
to Phase 5 once per-asset-class null tolerance is empirically measured.

Expected null rates per Alpaca documentation:
- common-stock-US: ~0-2% null
- ETF: ~30-50% null (Alpaca often omits)
- closed-end fund: ~40-60% null

Partial index `WHERE country IS NOT NULL` keeps the index small until
backfill is comprehensive.

Revision ID: 20260523_0601
Revises: 20260523_0600
Create Date: 2026-05-23
"""
import sqlalchemy as sa
from alembic import op

revision: str = "20260523_0601"
down_revision: str | None = "20260523_0600"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "ticker_classifications",
        sa.Column("country", sa.CHAR(length=2), nullable=True),
        schema="platform",
    )
    op.create_index(
        "idx_ticker_classifications_country",
        "ticker_classifications",
        ["country"],
        schema="platform",
        postgresql_where=sa.text("country IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_ticker_classifications_country",
        table_name="ticker_classifications",
        schema="platform",
    )
    op.drop_column("ticker_classifications", "country", schema="platform")
