"""prices_daily source col + platform.tradier_options_chains

Revision ID: 20260510_2330
Revises: 20260510_2300
Create Date: 2026-05-10

Two structural changes ahead of the Tradier closure ingest:

1. ``platform.prices_daily`` gains a ``source`` text column. Existing rows
   are backfilled to ``'alpaca'`` (every prior bar came from the Alpaca
   bootstrap or daily ingest). New Tradier rows land with ``source = 'tradier'``.

2. New table ``platform.tradier_options_chains`` to absorb the 122k-row
   options snapshot pulled from the Tradier production API right before
   the brokerage account closes. Primary key on (ticker, expiration_date,
   strike, option_type) gives us free dedup on idempotent re-loads. The
   table is read-only reference data for the future S2 engine — see
   master plan §4.4.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260510_2330"
down_revision: str | None = "20260510_2300"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. prices_daily.source
    op.add_column(
        "prices_daily",
        sa.Column("source", sa.Text, nullable=False, server_default=sa.text("'alpaca'")),
        schema="platform",
    )

    # 2. tradier_options_chains
    op.create_table(
        "tradier_options_chains",
        sa.Column("ticker", sa.Text, nullable=False),
        sa.Column("expiration_date", sa.Date, nullable=False),
        sa.Column("strike", sa.Numeric(20, 6), nullable=False),
        sa.Column("option_type", sa.Text, nullable=False),  # CALL | PUT
        sa.Column("bid", sa.Numeric(20, 6)),
        sa.Column("ask", sa.Numeric(20, 6)),
        sa.Column("last", sa.Numeric(20, 6)),
        sa.Column("volume", sa.BigInteger),
        sa.Column("open_interest", sa.BigInteger),
        sa.Column(
            "retrieved_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "ticker", "expiration_date", "strike", "option_type",
            name="pk_tradier_options_chains",
        ),
        sa.CheckConstraint(
            "option_type IN ('CALL', 'PUT')",
            name="ck_tradier_options_type",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_tradier_options_ticker_exp",
        "tradier_options_chains",
        ["ticker", "expiration_date"],
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tradier_options_ticker_exp",
        table_name="tradier_options_chains",
        schema="platform",
    )
    op.drop_table("tradier_options_chains", schema="platform")
    op.drop_column("prices_daily", "source", schema="platform")
