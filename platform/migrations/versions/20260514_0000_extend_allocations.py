"""extend platform.allocations for the Allocator service

Revision ID: 20260514_0000
Revises: 20260513_1237
Create Date: 2026-05-14

Allocator (MASTER_PLAN §5) needs to record both WHAT (target capital
per engine) and WHY (weight, realized vol, freeze state, drawdown) so
the operator can audit each weekly rebalance without re-running the
math. Original schema captured only the WHAT.

Columns added (all nullable for back-compat with existing 0 rows):

* ``weight``             — inverse-vol weight, normalized, in [0.10, 0.50]
* ``prior_equity``       — equity at decision time
* ``realized_vol``       — trailing-60-session daily-PnL std; NULL during bootstrap
* ``freeze_state``       — 'active' | 'soft_frozen' | 'hard_frozen'
* ``freeze_reason``      — human-readable cause
* ``drawdown_pct``       — current trailing-peak drawdown
* ``decided_at``         — TIMESTAMPTZ
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260514_0000"
down_revision: str | None = "20260513_1237"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_FREEZE_CHECK_NAME = "allocations_freeze_state_chk"


def upgrade() -> None:
    op.add_column("allocations", sa.Column("weight", sa.Numeric(6, 5), nullable=True), schema="platform")
    op.add_column("allocations", sa.Column("prior_equity", sa.Numeric(20, 4), nullable=True), schema="platform")
    op.add_column("allocations", sa.Column("realized_vol", sa.Numeric(20, 6), nullable=True), schema="platform")
    op.add_column(
        "allocations",
        sa.Column(
            "freeze_state", sa.Text, nullable=False,
            server_default=sa.text("'active'"),
            comment="'active' | 'soft_frozen' | 'hard_frozen'",
        ),
        schema="platform",
    )
    op.add_column("allocations", sa.Column("freeze_reason", sa.Text, nullable=True), schema="platform")
    op.add_column("allocations", sa.Column("drawdown_pct", sa.Numeric(6, 4), nullable=True), schema="platform")
    op.add_column(
        "allocations",
        sa.Column(
            "decided_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        schema="platform",
    )
    op.create_check_constraint(
        _FREEZE_CHECK_NAME,
        "allocations",
        "freeze_state IN ('active','soft_frozen','hard_frozen')",
        schema="platform",
    )


def downgrade() -> None:
    op.drop_constraint(_FREEZE_CHECK_NAME, "allocations", schema="platform")
    for col in ("decided_at", "drawdown_pct", "freeze_reason", "freeze_state",
                "realized_vol", "prior_equity", "weight"):
        op.drop_column("allocations", col, schema="platform")
