"""create platform.corporate_actions

Revision ID: 20260510_2300
Revises: 20260510_1200
Create Date: 2026-05-10

The corporate-actions ingest pulls splits and dividends from Alpaca's free
``/v1/corporate-actions`` endpoint and persists them here. Splits then drive
``tpcore.data.apply_splits``, which back-adjusts ``platform.prices_daily``
for tickers whose bar data Alpaca returned raw (notably AAPL on the IEX
free tier).

Schema follows the user's spec:

* ``ratio`` is dual-purpose: split factor (``new_rate / old_rate``) for
  splits, per-share USD amount for cash dividends. The action_type column
  disambiguates.
* ``raw_data`` retains the full Alpaca record (CUSIP, payable_date,
  record_date, etc.) for audit and future use without requiring a schema
  change every time we want a new field.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260510_2300"
down_revision: str | None = "20260510_1200"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "corporate_actions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.Text, nullable=False),
        sa.Column("action_date", sa.Date, nullable=False),
        sa.Column("action_type", sa.Text, nullable=False),
        sa.Column("ratio", sa.Numeric(20, 8), nullable=False),
        sa.Column("raw_data", sa.dialects.postgresql.JSONB, nullable=False),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "action_type IN ('split', 'dividend')",
            name="ck_corporate_actions_type",
        ),
        sa.UniqueConstraint(
            "ticker", "action_date", "action_type",
            name="uq_corporate_actions_ticker_date_type",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_corporate_actions_ticker_date",
        "corporate_actions",
        ["ticker", "action_date"],
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_corporate_actions_ticker_date",
        table_name="corporate_actions",
        schema="platform",
    )
    op.drop_table("corporate_actions", schema="platform")
