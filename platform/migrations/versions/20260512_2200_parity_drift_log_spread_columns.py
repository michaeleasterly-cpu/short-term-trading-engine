"""add spread_at_order_pct + spread_observed_at to platform.parity_drift_log

Revision ID: 20260512_2200
Revises: 20260512_2100
Create Date: 2026-05-12

B7: when LivePaperParityHarness logs a fill, it now also captures the
most-recent ``platform.spread_observations.spread_pct`` for the same
ticker. Two new columns:

* ``spread_at_order_pct`` — the spread (fraction of mid) observed for
  this ticker at the time of submission. NULL when no observation
  exists yet (e.g. first day for a new ticker before the bootstrap
  runs).
* ``spread_observed_at`` — the timestamp of that observation row, so
  drift analysis can tell stale-quote rows apart from fresh ones.

Both nullable so the column add is safe against the rows already in
the table.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260512_2200"
down_revision: str | None = "20260512_2100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "parity_drift_log",
        sa.Column("spread_at_order_pct", sa.Numeric(12, 6), nullable=True),
        schema="platform",
    )
    op.add_column(
        "parity_drift_log",
        sa.Column("spread_observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        schema="platform",
    )


def downgrade() -> None:
    op.drop_column("parity_drift_log", "spread_observed_at", schema="platform")
    op.drop_column("parity_drift_log", "spread_at_order_pct", schema="platform")
