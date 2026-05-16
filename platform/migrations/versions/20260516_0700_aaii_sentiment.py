"""aaii_sentiment — AAII weekly Sentiment Survey (no auth).

Built 2026-05-16. Weekly (published Thursdays). One row per survey
date; the source is a single full-history workbook so the upsert is
idempotent + self-correcting (ON CONFLICT (date) DO UPDATE). Each
weekly row's bull+neutral+bear sums to ~100 — a per-row CHECK keeps
a corrupt ingest from landing.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260516_0700"
down_revision: str | None = "20260516_0600"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "aaii_sentiment",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("bullish_pct", sa.Numeric(6, 2), nullable=False),
        sa.Column("bearish_pct", sa.Numeric(6, 2), nullable=False),
        sa.Column("neutral_pct", sa.Numeric(6, 2), nullable=False),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("date", name="aaii_sentiment_pk"),
        sa.CheckConstraint(
            "bullish_pct >= 0 AND bullish_pct <= 100",
            name="aaii_sentiment_bull_range_chk",
        ),
        sa.CheckConstraint(
            "bearish_pct >= 0 AND bearish_pct <= 100",
            name="aaii_sentiment_bear_range_chk",
        ),
        sa.CheckConstraint(
            "neutral_pct >= 0 AND neutral_pct <= 100",
            name="aaii_sentiment_neu_range_chk",
        ),
        sa.CheckConstraint(
            "abs(bullish_pct + bearish_pct + neutral_pct - 100) <= 1.5",
            name="aaii_sentiment_sum_chk",
        ),
        sa.CheckConstraint(
            "date <= CURRENT_DATE + INTERVAL '1 day'",
            name="aaii_sentiment_no_future_chk",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_aaii_sentiment_date",
        "aaii_sentiment",
        ["date"],
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_aaii_sentiment_date",
        table_name="aaii_sentiment",
        schema="platform",
    )
    op.drop_table("aaii_sentiment", schema="platform")
