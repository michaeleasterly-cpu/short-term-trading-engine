"""insider_sentiment — Finnhub free-tier MSPR ingest table.

Built 2026-05-16. Finnhub free tier (``FINNHUB_API_KEY``) exposes
``/stock/insider-sentiment``: monthly MSPR (Monthly Share Purchase
Ratio, insider sentiment, [-100,100]) + net insider share change per
symbol. ``/news-sentiment`` / ``/stock/social-sentiment`` are premium
(403 on free, verified) and not ingested.

One row per (symbol, year, month) so re-runs are idempotent under
``ON CONFLICT DO NOTHING``.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260516_0200"
down_revision: str | None = "20260516_0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "insider_sentiment",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("mspr", sa.Numeric(12, 6), nullable=False),
        sa.Column("net_change", sa.Numeric(20, 2), nullable=False),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "symbol", "year", "month", name="insider_sentiment_pk",
        ),
        sa.CheckConstraint("length(symbol) > 0", name="insider_sentiment_symbol_chk"),
        sa.CheckConstraint("month BETWEEN 1 AND 12", name="insider_sentiment_month_chk"),
        sa.CheckConstraint(
            "mspr BETWEEN -100 AND 100", name="insider_sentiment_mspr_chk",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_insider_sentiment_symbol_period",
        "insider_sentiment",
        ["symbol", "year", "month"],
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_insider_sentiment_symbol_period",
        table_name="insider_sentiment",
        schema="platform",
    )
    op.drop_table("insider_sentiment", schema="platform")
