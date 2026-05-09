"""create platform.fundamentals_quarterly

Revision ID: 20260510_0049
Revises: 20260509_1956
Create Date: 2026-05-10 00:49

Per-ticker quarterly fundamentals cache. Populated by
``tpcore.fundamentals.cache.FundamentalsCache``, which wraps the FMP
adapter — engines hit this table first to avoid burning the FMP quota
on every scheduler run.

Idempotent on ``(ticker, filing_date)`` so re-fetching the same FMP
response is a no-op. PIT queries use the index on
``(ticker, filing_date)``.
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260510_0049"
down_revision: str | None = "20260509_1956"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fundamentals_quarterly",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.Text, nullable=False),
        sa.Column("filing_date", sa.Date, nullable=False),
        sa.Column("period_end_date", sa.Date, nullable=False),
        sa.Column("period_label", sa.Text),  # e.g. "Q4" — handy for YoY lookups
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
        sa.UniqueConstraint("ticker", "filing_date", name="uq_fundamentals_ticker_filing"),
        schema="platform",
    )
    op.create_index(
        "ix_fundamentals_ticker_filing",
        "fundamentals_quarterly",
        ["ticker", "filing_date"],
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fundamentals_ticker_filing",
        table_name="fundamentals_quarterly",
        schema="platform",
    )
    op.drop_table("fundamentals_quarterly", schema="platform")
