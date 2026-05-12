"""create platform.open_orders

Revision ID: 20260512_0000
Revises: 20260511_0200
Create Date: 2026-05-12

Storage for in-flight broker orders that the live trade monitor
(``tpcore/trade_monitor.py``) reads to decide when to submit Tier 2,
when to cancel, and when to write the AAR. The engines write Tier 1
rows here on submission; the monitor adds Tier 2 rows reactively after
the Tier 1 fill arrives via Alpaca's ``trade_updates`` stream.

Why a dedicated table (not just ``platform.application_log``):

* The monitor needs a fast point-lookup by ``alpaca_order_id`` on every
  inbound fill event; the audit log isn't indexed that way.
* The full ``ExecutionDecision`` + ``PhaseAssessment`` are persisted as
  ``decision_data`` JSONB so the monitor can rebuild the Tier 2 payload
  from the same authoritative inputs the engine used — no replay through
  setup_detection on the monitor's hot path.
* Crash safety: on restart the monitor reads every row in 'pending' state
  and reconciles against ``broker.get_order(alpaca_order_id)`` to catch
  state drift that happened while the monitor was down.

Lifecycle (per row):
    pending     - order submitted to Alpaca, no terminal event yet
    filled      - fill event received; ``fill_price`` + ``filled_at`` set
    cancelled   - cancellation confirmed by broker
    rejected    - broker rejected the submission

Unique (engine, trade_id, order_type) keeps a single 'tier1' / 'tier2'
row per trade.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260512_0000"
down_revision: str | None = "20260511_0200"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "open_orders",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("engine", sa.Text, nullable=False),
        sa.Column(
            "trade_id",
            sa.Text,
            nullable=False,
            comment="Engine-side trade key (ticker_timestamp). Same for the matching tier1 + tier2 rows.",
        ),
        sa.Column("ticker", sa.Text, nullable=False),
        sa.Column(
            "order_type",
            sa.Text,
            nullable=False,
            comment="'tier1' (bracket entry) or 'tier2' (scale-out, submitted reactively by trade monitor).",
        ),
        sa.Column(
            "alpaca_order_id",
            sa.Text,
            nullable=True,
            comment="Broker-assigned order id, NULL until the place_order call returns.",
        ),
        sa.Column(
            "status",
            sa.Text,
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("fill_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("filled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "decision_data",
            postgresql.JSONB,
            nullable=False,
            comment=(
                "Frozen snapshot of {'decision': ExecutionDecision, 'assessment': PhaseAssessment}. "
                "Monitor reads this to build the Tier 2 payload after Tier 1 fill."
            ),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("engine", "trade_id", "order_type", name="open_orders_engine_trade_type_uq"),
        sa.CheckConstraint(
            "order_type IN ('tier1','tier2')",
            name="open_orders_order_type_chk",
        ),
        sa.CheckConstraint(
            "status IN ('pending','filled','cancelled','rejected')",
            name="open_orders_status_chk",
        ),
        schema="platform",
    )
    # Fast lookup by broker id (every inbound stream event hits this).
    op.create_index(
        "open_orders_alpaca_order_id_idx",
        "open_orders",
        ["alpaca_order_id"],
        unique=False,
        schema="platform",
        postgresql_where=sa.text("alpaca_order_id IS NOT NULL"),
    )
    # Active-position scans for risk reconciliation.
    op.create_index(
        "open_orders_engine_status_idx",
        "open_orders",
        ["engine", "status"],
        unique=False,
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "open_orders_engine_status_idx", table_name="open_orders", schema="platform"
    )
    op.drop_index(
        "open_orders_alpaca_order_id_idx", table_name="open_orders", schema="platform"
    )
    op.drop_table("open_orders", schema="platform")
