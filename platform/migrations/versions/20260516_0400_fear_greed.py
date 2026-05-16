"""fear_greed — internally-computed 4-component Fear & Greed index.

Built 2026-05-16. One row per trading date. Derived purely from data
the platform already has (FRED vix/hy_spread/yield_curve +
prices_daily SPY) via ``tpcore.indicators.fear_greed`` — no external
provider. ``ON CONFLICT (date) DO UPDATE`` so a recompute (e.g. after
late macro data lands or a window fills) corrects the row; re-running
is idempotent in final state.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260516_0400"
down_revision: str | None = "20260516_0300"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fear_greed",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("score", sa.Numeric(5, 1), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=True),
        sa.Column("score_5d_ago", sa.Numeric(5, 1), nullable=True),
        sa.Column("volatility_component", sa.Numeric(6, 2), nullable=False),
        sa.Column("credit_component", sa.Numeric(6, 2), nullable=False),
        sa.Column("momentum_component", sa.Numeric(6, 2), nullable=False),
        sa.Column("safe_haven_component", sa.Numeric(6, 2), nullable=False),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("date", name="fear_greed_pk"),
        sa.CheckConstraint("score >= 0 AND score <= 100", name="fear_greed_score_chk"),
        sa.CheckConstraint(
            "label IN ('Extreme Fear','Fear','Neutral','Greed','Extreme Greed')",
            name="fear_greed_label_chk",
        ),
        sa.CheckConstraint(
            "date <= CURRENT_DATE + INTERVAL '1 day'",
            name="fear_greed_no_future_chk",
        ),
        schema="platform",
    )


def downgrade() -> None:
    op.drop_table("fear_greed", schema="platform")
