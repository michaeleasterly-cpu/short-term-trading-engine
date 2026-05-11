"""initial platform schema

Revision ID: 20260509_0000
Revises:
Create Date: 2026-05-09

Creates the ``platform`` schema and all cross-engine tables: AAR events,
quality logs, parity drift, risk state, allocations, coroner triggers,
tax lots, and the daily prices table consumed by the backtest harness.
All timestamps are ``TIMESTAMPTZ``.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260509_0000"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS platform")

    op.create_table(
        "aar_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("engine", sa.Text, nullable=False),
        sa.Column("trade_id", sa.Text, nullable=False),
        sa.Column("ticker", sa.Text, nullable=False),
        sa.Column("aar_data", sa.dialects.postgresql.JSONB, nullable=False),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("engine", "trade_id", name="uq_aar_events_engine_trade"),
        schema="platform",
    )
    op.create_index(
        "ix_aar_events_ticker_recorded",
        "aar_events",
        ["ticker", "recorded_at"],
        schema="platform",
    )

    op.create_table(
        "execution_quality_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("broker", sa.Text, nullable=False),
        sa.Column("order_id", sa.Text, nullable=False),
        sa.Column("requested_price", sa.Numeric(20, 6)),
        sa.Column("fill_price", sa.Numeric(20, 6), nullable=False),
        sa.Column("slippage_bps", sa.Numeric(10, 4), nullable=False),
        sa.Column("partial_fill", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("paper_or_live", sa.Text, nullable=False),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("notes", sa.Text),
        sa.UniqueConstraint("broker", "order_id", name="uq_exq_broker_order"),
        schema="platform",
    )

    op.create_table(
        "data_quality_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("latency_ms", sa.Integer, nullable=False),
        sa.Column("missing_bars", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("stale", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
        sa.Column("notes", sa.Text),
        schema="platform",
    )
    op.create_index(
        "ix_dq_source_ts",
        "data_quality_log",
        ["source", "timestamp"],
        schema="platform",
    )

    op.create_table(
        "parity_drift_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("client_order_id", sa.Text, nullable=False),
        sa.Column("paper_fill_price", sa.Numeric(20, 6)),
        sa.Column("live_fill_price", sa.Numeric(20, 6)),
        sa.Column("drift_bps", sa.Numeric(12, 4)),
        sa.Column("paper_filled_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("live_filled_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False),
        schema="platform",
    )

    op.create_table(
        "risk_state",
        sa.Column("engine", sa.Text, primary_key=True),
        sa.Column("engine_equity", sa.Numeric(20, 4), nullable=False),
        sa.Column("daily_pnl", sa.Numeric(20, 4), nullable=False, server_default=sa.text("0")),
        sa.Column("weekly_pnl", sa.Numeric(20, 4), nullable=False, server_default=sa.text("0")),
        sa.Column("open_positions", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("daily_reset_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("weekly_reset_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "kill_switch_active",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("kill_switch_reason", sa.Text),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        schema="platform",
    )

    # Stub for the future Commander allocator.
    op.create_table(
        "allocations",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("engine", sa.Text, nullable=False),
        sa.Column("allocated_capital", sa.Numeric(20, 4), nullable=False),
        sa.Column("allocation_date", sa.Date, nullable=False),
        sa.UniqueConstraint("engine", "allocation_date", name="uq_alloc_engine_date"),
        schema="platform",
    )

    # Stub for the future Forensics service.
    op.create_table(
        "coroner_triggers",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("trigger_kind", sa.Text, nullable=False),
        sa.Column("payload", sa.dialects.postgresql.JSONB, nullable=False),
        sa.Column(
            "fired_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True)),
        schema="platform",
    )

    op.create_table(
        "tax_lots",
        sa.Column("lot_id", sa.Text, primary_key=True),
        sa.Column("ticker", sa.Text, nullable=False),
        sa.Column("engine_id", sa.Text, nullable=False),
        sa.Column("acquisition_date", sa.Date, nullable=False),
        sa.Column("shares", sa.Numeric(20, 6), nullable=False),
        sa.Column("cost_basis", sa.Numeric(20, 6), nullable=False),
        sa.Column("lot_status", sa.Text, nullable=False, server_default=sa.text("'open'")),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("realized_pnl", sa.Numeric(20, 6)),
        sa.CheckConstraint(
            "lot_status IN ('open', 'closed', 'partial')",
            name="ck_tax_lots_status",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_tax_lots_ticker_status",
        "tax_lots",
        ["ticker", "lot_status"],
        schema="platform",
    )

    # Daily price store consumed by the backtest harness + ingestion script.
    op.create_table(
        "prices_daily",
        sa.Column("ticker", sa.Text, primary_key=True),
        sa.Column("date", sa.Date, primary_key=True),
        sa.Column("open", sa.Numeric(20, 6), nullable=False),
        sa.Column("high", sa.Numeric(20, 6), nullable=False),
        sa.Column("low", sa.Numeric(20, 6), nullable=False),
        sa.Column("close", sa.Numeric(20, 6), nullable=False),
        sa.Column("volume", sa.BigInteger, nullable=False),
        sa.Column("adjusted_close", sa.Numeric(20, 6)),
        sa.Column("delisted", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("delisting_date", sa.Date),
        schema="platform",
    )


def downgrade() -> None:
    op.drop_table("prices_daily", schema="platform")
    op.drop_index("ix_tax_lots_ticker_status", table_name="tax_lots", schema="platform")
    op.drop_table("tax_lots", schema="platform")
    op.drop_table("coroner_triggers", schema="platform")
    op.drop_table("allocations", schema="platform")
    op.drop_table("risk_state", schema="platform")
    op.drop_table("parity_drift_log", schema="platform")
    op.drop_index("ix_dq_source_ts", table_name="data_quality_log", schema="platform")
    op.drop_table("data_quality_log", schema="platform")
    op.drop_table("execution_quality_log", schema="platform")
    op.drop_index("ix_aar_events_ticker_recorded", table_name="aar_events", schema="platform")
    op.drop_table("aar_events", schema="platform")
    op.execute("DROP SCHEMA IF EXISTS platform")
