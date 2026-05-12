"""create platform.spread_observations + platform.liquidity_tiers

Revision ID: 20260512_2100
Revises: 20260512_0000
Create Date: 2026-05-12

Phase 2 cost-model storage. Two tables:

* ``spread_observations`` — every bid-ask quote we record. Streaming
  Tradier rows land here as ``source = 'tradier_streaming'``; the
  Corwin-Schultz daily-bar bootstrap also writes here as
  ``source = 'corwin_schultz'`` for reference only. 30-day rolling
  retention enforced server-side.
* ``liquidity_tiers`` — one row per ticker with the tier assignment
  computed weekly by ``scripts/assign_liquidity_tiers.py`` from the
  streaming observations. Only ``tradier_streaming`` data feeds the
  tier; the Corwin-Schultz bootstrap is a prioritisation hint, not a
  source of truth.

Retention strategy
------------------
``spread_observations`` rows older than 30 days are dropped on every
INSERT via a row-level trigger (cheap, no cron dependency, matches
the same pattern ``platform.application_log`` uses).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260512_2100"
down_revision: str | None = "20260512_0000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "spread_observations",
        sa.Column(
            "id",
            sa.BigInteger,
            sa.Identity(always=False),
            primary_key=True,
        ),
        sa.Column("ticker", sa.Text, nullable=False),
        sa.Column(
            "spread_pct",
            sa.Numeric(12, 6),
            nullable=False,
            comment="Bid-ask spread as a fraction of mid-price. 0.0015 = 15 bps.",
        ),
        sa.Column(
            "observed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            comment="Quote timestamp (UTC).",
        ),
        sa.Column(
            "session",
            sa.Text,
            nullable=False,
            comment="'regular' | 'pre_market' | 'after_hours'.",
        ),
        sa.Column(
            "source",
            sa.Text,
            nullable=False,
            comment="'tradier_streaming' (authoritative) | 'corwin_schultz' (bootstrap).",
        ),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "session IN ('regular','pre_market','after_hours')",
            name="spread_observations_session_chk",
        ),
        sa.CheckConstraint(
            "source IN ('tradier_streaming','corwin_schultz')",
            name="spread_observations_source_chk",
        ),
        sa.CheckConstraint(
            "spread_pct >= 0",
            name="spread_observations_nonneg_chk",
        ),
        schema="platform",
    )
    op.create_index(
        "spread_observations_ticker_observed_idx",
        "spread_observations",
        ["ticker", "observed_at"],
        unique=False,
        schema="platform",
    )
    op.create_index(
        "spread_observations_source_observed_idx",
        "spread_observations",
        ["source", "observed_at"],
        unique=False,
        schema="platform",
    )
    # 30-day rolling retention — same shape as platform.application_log.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION platform._spread_observations_retention()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
            DELETE FROM platform.spread_observations
            WHERE observed_at < now() - INTERVAL '30 days';
            RETURN NULL;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER spread_observations_retention_trg
        AFTER INSERT ON platform.spread_observations
        FOR EACH STATEMENT
        EXECUTE FUNCTION platform._spread_observations_retention();
        """
    )

    op.create_table(
        "liquidity_tiers",
        sa.Column("ticker", sa.Text, primary_key=True),
        sa.Column("tier", sa.Integer, nullable=False),
        sa.Column("median_spread_pct", sa.Numeric(12, 6), nullable=False),
        sa.Column("p95_spread_pct", sa.Numeric(12, 6), nullable=False),
        sa.Column("observations", sa.Integer, nullable=False),
        sa.Column(
            "provisional",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
            comment="True until 100+ observations AND 5+ trading days of data.",
        ),
        sa.Column(
            "last_updated",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "tier BETWEEN 1 AND 5",
            name="liquidity_tiers_tier_range_chk",
        ),
        sa.CheckConstraint(
            "median_spread_pct >= 0 AND p95_spread_pct >= 0",
            name="liquidity_tiers_nonneg_chk",
        ),
        sa.CheckConstraint(
            "observations >= 0",
            name="liquidity_tiers_observations_nonneg_chk",
        ),
        schema="platform",
    )


def downgrade() -> None:
    op.drop_table("liquidity_tiers", schema="platform")
    op.execute("DROP TRIGGER IF EXISTS spread_observations_retention_trg ON platform.spread_observations")
    op.execute("DROP FUNCTION IF EXISTS platform._spread_observations_retention()")
    op.drop_index(
        "spread_observations_source_observed_idx",
        table_name="spread_observations",
        schema="platform",
    )
    op.drop_index(
        "spread_observations_ticker_observed_idx",
        table_name="spread_observations",
        schema="platform",
    )
    op.drop_table("spread_observations", schema="platform")
