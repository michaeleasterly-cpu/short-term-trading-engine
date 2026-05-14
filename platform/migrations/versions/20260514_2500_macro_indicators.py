"""macro_indicators — FRED macro time-series ingest table.

Built 2026-05-14. The FRED adapter (``tpcore/fred/adapter.py``) is the
last data source from MASTER_PLAN §6.1 — closes the "spec-only" gap
and unblocks the Sentinel macro-defense engine.

One row per (indicator, observation date). FRED returns daily/weekly/
monthly observations depending on the series; the table is agnostic.
The CHECK constraint mirrors FRED's NULL convention (FRED uses "." for
missing values; the loader filters those out so ``value IS NULL`` should
never happen).

Indicators (all free, no key beyond the FRED API key):

* ``sahm_rule``        — SAHMREALTIME, monthly recession indicator
* ``industrial_production`` — INDPRO, monthly PMI proxy
* ``initial_claims``   — IC4WSA, weekly 4-wk MA of jobless claims
* ``yield_curve``      — T10Y2Y, daily 10y-2y Treasury spread
* ``hy_spread``        — BAMLH0A0HYM2, daily HY OAS (credit stress)
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260514_2500"
down_revision: str | None = "20260514_2400"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "macro_indicators",
        sa.Column("indicator", sa.Text(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric(20, 6), nullable=False),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("indicator", "date", name="macro_indicators_pk"),
        sa.CheckConstraint("length(indicator) > 0", name="macro_indicators_name_chk"),
        sa.CheckConstraint(
            "date <= CURRENT_DATE + INTERVAL '1 day'",
            name="macro_indicators_no_future_dates_chk",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_macro_indicators_indicator_date",
        "macro_indicators",
        ["indicator", "date"],
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_macro_indicators_indicator_date",
        table_name="macro_indicators",
        schema="platform",
    )
    op.drop_table("macro_indicators", schema="platform")
