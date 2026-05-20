"""fundamentals.pb/de + catalyst_events

Revision ID: 20260511_0000
Revises: 20260510_2330
Create Date: 2026-05-11

Two structural changes for Vector's three-gate backtest:

1. ``platform.fundamentals_quarterly`` gains ``pb`` and ``de`` columns —
   point-in-time book-to-price and debt-to-equity ratios. Both are
   nullable; ``ops.py --stage compute_fundamental_ratios`` populates them
   using ``close`` on each row's ``filing_date`` (or nearest prior
   trading day).

2. New table ``platform.catalyst_events`` for the MVP catalyst proxy
   (Vector Gate 2). Columns:
       ticker, event_date, event_type, magnitude_pct, source, recorded_at
   PK on (ticker, event_date, event_type) keeps the backfill idempotent.
   Only ``EARNINGS_BEAT`` is populated for MVP; the type column is
   forward-looking so contract-award and guidance-raise rows can land
   later without another migration.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260511_0000"
down_revision: str | None = "20260510_2330"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. fundamentals_quarterly.pb / .de
    op.add_column(
        "fundamentals_quarterly",
        sa.Column("pb", sa.Numeric(20, 6), nullable=True),
        schema="platform",
    )
    op.add_column(
        "fundamentals_quarterly",
        sa.Column("de", sa.Numeric(20, 6), nullable=True),
        schema="platform",
    )

    # 2. catalyst_events
    op.create_table(
        "catalyst_events",
        sa.Column("ticker", sa.Text, nullable=False),
        sa.Column("event_date", sa.Date, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("magnitude_pct", sa.Numeric(20, 6)),
        sa.Column("source", sa.Text, nullable=False, server_default=sa.text("'fmp'")),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "ticker", "event_date", "event_type",
            name="pk_catalyst_events",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_catalyst_events_ticker_date",
        "catalyst_events",
        ["ticker", "event_date"],
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_catalyst_events_ticker_date",
        table_name="catalyst_events",
        schema="platform",
    )
    op.drop_table("catalyst_events", schema="platform")
    op.drop_column("fundamentals_quarterly", "de", schema="platform")
    op.drop_column("fundamentals_quarterly", "pb", schema="platform")
