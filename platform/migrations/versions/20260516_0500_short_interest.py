"""short_interest — FINRA consolidated short interest (PIT-safe).

Built 2026-05-16. Bi-monthly. ``release_date`` is stored SEPARATELY
from ``settlement_date`` for point-in-time correctness — backtests
filter ``release_date <= simulation_date`` (FINRA disseminates ~8-9
business days after settlement; the handler sets release_date to a
conservative settlement + NYSE-session lag). ``short_interest_pct`` is
derived from ``fundamentals_quarterly.shares_outstanding`` (FINRA does
not publish float); NULL when shares_outstanding is unavailable PIT.
One row per (ticker, settlement_date), idempotent ON CONFLICT.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260516_0500"
down_revision: str | None = "20260516_0400"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "short_interest",
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("settlement_date", sa.Date(), nullable=False),
        sa.Column("release_date", sa.Date(), nullable=False),
        sa.Column("short_interest_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("days_to_cover", sa.Numeric(10, 2), nullable=True),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "ticker", "settlement_date", name="short_interest_pk",
        ),
        sa.CheckConstraint("length(ticker) > 0", name="short_interest_ticker_chk"),
        sa.CheckConstraint(
            "release_date >= settlement_date",
            name="short_interest_release_after_settle_chk",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_short_interest_release",
        "short_interest",
        ["release_date", "ticker"],
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_short_interest_release",
        table_name="short_interest",
        schema="platform",
    )
    op.drop_table("short_interest", schema="platform")
