"""create platform.ingestion_jobs

Revision ID: 20260511_0200
Revises: 20260511_0100
Create Date: 2026-05-10

Schedule + state for the unified ingestion engine. The engine is a
persistent worker (``ops/ingestion_engine.py``) that wakes every 60s,
selects rows with ``enabled = true AND next_run <= now()``, claims them
via a guarded UPDATE, dispatches by ``job_name``, and writes results
back. Replaces the family of single-purpose Sunday/MON-FRI cron services.

Schema notes:

* ``job_name`` is the primary key — handlers are registered by name in
  ``tpcore.ingestion.handlers``.
* ``schedule`` is a standard 5-field cron expression (parsed via
  ``croniter``). Stored as text so we can edit on the fly without a
  migration.
* ``config`` is jsonb so each job can carry handler-specific knobs
  (universe selection, lookback windows) without schema churn.
* ``last_status`` doubles as a soft lock: rows with ``'running'`` are
  skipped by the next tick. A 30-minute staleness escape hatch in the
  engine recovers from a process crash.
* ``next_run`` defaults to ``now()`` so freshly-seeded rows fire on the
  next tick — no manual priming needed.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260511_0200"
down_revision: str | None = "20260511_0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingestion_jobs",
        sa.Column("job_name", sa.Text, primary_key=True),
        sa.Column("schedule", sa.Text, nullable=False),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column(
            "config",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "enabled", sa.Boolean, nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "next_run",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_run_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("last_status", sa.Text),
        sa.Column("last_error", sa.Text),
        sa.Column("last_duration_ms", sa.Integer),
        sa.Column(
            "recorded_at",
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
        sa.CheckConstraint(
            "last_status IS NULL OR last_status IN ('success', 'failed', 'running')",
            name="ck_ingestion_jobs_last_status",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_ingestion_jobs_due",
        "ingestion_jobs",
        ["next_run"],
        postgresql_where=sa.text("enabled = true"),
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ingestion_jobs_due",
        table_name="ingestion_jobs",
        schema="platform",
    )
    op.drop_table("ingestion_jobs", schema="platform")
