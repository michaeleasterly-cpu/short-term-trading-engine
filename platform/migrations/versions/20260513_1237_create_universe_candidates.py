"""create platform.universe_candidates

Revision ID: 20260513_1237
Revises: 20260512_2200
Create Date: 2026-05-13

Daily-refreshed per-engine candidate roster. Each row says "ticker X was in
scope for engine Y on date D". The table answers "what's in scope today",
not "what's ranked" — engines compute their own scores at runtime.

V1 populates only ``engine='momentum'`` rows. Other engines keep their
hardcoded universes until they need this.

Primary key (as_of_date, engine, ticker) makes upserts trivial and
guarantees no double-counting per day.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260513_1237"
down_revision: str | None = "20260512_2200"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "universe_candidates",
        sa.Column("as_of_date", sa.Date, nullable=False),
        sa.Column(
            "engine",
            sa.Text,
            nullable=False,
            comment="Engine name: 'momentum', 'sigma', 'reversion', 'vector', etc.",
        ),
        sa.Column("ticker", sa.Text, nullable=False),
        sa.Column(
            "tier",
            sa.SmallInteger,
            nullable=True,
            comment="Liquidity tier at selection time (1-5); null if engine doesn't gate on tier.",
        ),
        sa.Column(
            "last_close",
            sa.Numeric(18, 6),
            nullable=True,
            comment="Most recent close used for the tradability check.",
        ),
        sa.Column(
            "reason",
            sa.Text,
            nullable=True,
            comment="Optional short note about why this ticker was included.",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "as_of_date",
            "engine",
            "ticker",
            name="universe_candidates_pkey",
        ),
        sa.CheckConstraint(
            "tier IS NULL OR tier BETWEEN 1 AND 5",
            name="universe_candidates_tier_range_chk",
        ),
        schema="platform",
    )
    op.create_index(
        "idx_uc_engine_date",
        "universe_candidates",
        ["engine", "as_of_date"],
        unique=False,
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "idx_uc_engine_date",
        table_name="universe_candidates",
        schema="platform",
    )
    op.drop_table("universe_candidates", schema="platform")
