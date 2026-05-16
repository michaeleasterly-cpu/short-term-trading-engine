"""borrow_rates — IBorrowDesk daily borrow-fee (no auth).

Built 2026-05-16. Daily. One row per (ticker, date), idempotent
ON CONFLICT. Scrape-fragile source — the handler skips (never crashes)
on repeated blocks; this table simply holds whatever was reachable.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260516_0600"
down_revision: str | None = "20260516_0500"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "borrow_rates",
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("borrow_rate_pct", sa.Numeric(12, 4), nullable=False),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("ticker", "date", name="borrow_rates_pk"),
        sa.CheckConstraint("length(ticker) > 0", name="borrow_rates_ticker_chk"),
        sa.CheckConstraint("borrow_rate_pct >= 0", name="borrow_rates_nonneg_chk"),
        sa.CheckConstraint(
            "date <= CURRENT_DATE + INTERVAL '1 day'",
            name="borrow_rates_no_future_chk",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_borrow_rates_date_ticker",
        "borrow_rates",
        ["date", "ticker"],
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_borrow_rates_date_ticker",
        table_name="borrow_rates",
        schema="platform",
    )
    op.drop_table("borrow_rates", schema="platform")
