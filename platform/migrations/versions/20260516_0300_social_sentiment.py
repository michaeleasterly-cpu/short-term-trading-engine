"""social_sentiment — ApeWisdom Reddit social-sentiment ingest table.

Built 2026-05-16. ApeWisdom (no auth) scans Reddit communities every
~2h. One row per (ticker, date) — the day's snapshot of mentions /
upvotes / rank + the 24h-ago comparators. Unique (ticker, date) so
re-runs within a day are idempotent under ``ON CONFLICT DO NOTHING``.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260516_0300"
down_revision: str | None = "20260516_0200"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "social_sentiment",
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("mentions", sa.Integer(), nullable=False),
        sa.Column("upvotes", sa.Integer(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("rank_24h_ago", sa.Integer(), nullable=True),
        sa.Column("mentions_24h_ago", sa.Integer(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("ticker", "date", name="social_sentiment_pk"),
        sa.CheckConstraint("length(ticker) > 0", name="social_sentiment_ticker_chk"),
        sa.CheckConstraint("mentions >= 0", name="social_sentiment_mentions_chk"),
        sa.CheckConstraint(
            "date <= CURRENT_DATE + INTERVAL '1 day'",
            name="social_sentiment_no_future_chk",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_social_sentiment_date_ticker",
        "social_sentiment",
        ["date", "ticker"],
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_social_sentiment_date_ticker",
        table_name="social_sentiment",
        schema="platform",
    )
    op.drop_table("social_sentiment", schema="platform")
