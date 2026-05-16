"""options_max_pain — greeks.pro free-tier max-pain ingest table.

Built 2026-05-16. greeks.pro free tier (``GREEKS_API_KEY``) exposes
only ``/api/analytics/maxpain`` (10 req/min, 600 req/day, 1 symbol);
``/flow`` / ``/greeks`` / ``/gex`` are Trader+ (paid, verified 403).
This table holds the daily max-pain snapshot per (symbol, expiration).

One row per (symbol, expiration_date, observed_date) so a same-day
re-run is idempotent under ``ON CONFLICT DO NOTHING``. ``observed_at``
preserves the exact provider snapshot time; ``observed_date`` is its
UTC date and is the stable daily key.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260516_0100"
down_revision: str | None = "20260515_0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "options_max_pain",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("expiration_date", sa.Date(), nullable=False),
        sa.Column("observed_date", sa.Date(), nullable=False),
        sa.Column("dte", sa.Integer(), nullable=False),
        sa.Column("spot_price", sa.Numeric(20, 6), nullable=False),
        sa.Column("max_pain_strike", sa.Numeric(20, 6), nullable=False),
        sa.Column("total_pain_at_max", sa.Numeric(28, 6), nullable=False),
        sa.Column("spot_distance", sa.Numeric(20, 6), nullable=False),
        sa.Column("spot_distance_pct", sa.Numeric(12, 6), nullable=False),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "symbol", "expiration_date", "observed_date",
            name="options_max_pain_pk",
        ),
        sa.CheckConstraint("length(symbol) > 0", name="options_max_pain_symbol_chk"),
        sa.CheckConstraint("spot_price > 0", name="options_max_pain_spot_chk"),
        sa.CheckConstraint(
            "observed_date <= CURRENT_DATE + INTERVAL '1 day'",
            name="options_max_pain_no_future_chk",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_options_max_pain_symbol_observed",
        "options_max_pain",
        ["symbol", "observed_date"],
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_options_max_pain_symbol_observed",
        table_name="options_max_pain",
        schema="platform",
    )
    op.drop_table("options_max_pain", schema="platform")
