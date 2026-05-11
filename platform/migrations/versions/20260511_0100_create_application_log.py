"""create platform.application_log

Revision ID: 20260511_0100
Revises: 20260511_0000
Create Date: 2026-05-10

Lightweight audit trail for engine scheduler runs. Every run inserts a
STARTUP / SCAN_COMPLETE / SIGNAL / ORDER_SUBMITTED / FILL_CONFIRMED /
ERROR / SHUTDOWN sequence, tagged with the engine and a per-run UUID so
the timeline of a single invocation is queryable.

Retention is enforced by ``DBLogHandler`` on every write — see
``tpcore/logging/db_handler.py``. The handler issues a DELETE for rows
older than its ``retention_days`` window after each insert, so the table
size stays bounded without a separate cron. Default window is 7 days.

Indexed on ``(engine, run_id, recorded_at)`` to support the canonical
"show me everything for this run" query and the retention DELETE.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260511_0100"
down_revision: str | None = "20260511_0000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "application_log",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("engine", sa.Text, nullable=False),
        sa.Column("run_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("severity", sa.Text, nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("data", sa.dialects.postgresql.JSONB),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        schema="platform",
    )
    op.create_index(
        "ix_application_log_engine_run_recorded",
        "application_log",
        ["engine", "run_id", "recorded_at"],
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_application_log_engine_run_recorded",
        table_name="application_log",
        schema="platform",
    )
    op.drop_table("application_log", schema="platform")
