"""extend spread_observations.source CHECK to allow abdi_ranaldo

Revision ID: 20260515_0100
Revises: 20260515_0000
Create Date: 2026-05-15

The Abdi-Ranaldo (2017) spread estimator replaced Corwin-Schultz as
the active estimator on 2026-05-15. The original CHECK constraint
(``source IN ('tradier_streaming', 'corwin_schultz')``) prevented
writing rows tagged ``abdi_ranaldo``. This migration extends the
allowed set without removing the legacy ``corwin_schultz`` value so
historical rows aren't orphaned.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260515_0100"
down_revision: str | None = "20260515_0000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "spread_observations_source_chk",
        "spread_observations",
        schema="platform",
    )
    op.create_check_constraint(
        "spread_observations_source_chk",
        "spread_observations",
        "source IN ('tradier_streaming', 'corwin_schultz', 'abdi_ranaldo')",
        schema="platform",
    )


def downgrade() -> None:
    op.drop_constraint(
        "spread_observations_source_chk",
        "spread_observations",
        schema="platform",
    )
    op.create_check_constraint(
        "spread_observations_source_chk",
        "spread_observations",
        "source IN ('tradier_streaming', 'corwin_schultz')",
        schema="platform",
    )
