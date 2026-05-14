"""create platform.daemon_heartbeats

Revision ID: 20260515_0000
Revises: 20260514_2500
Create Date: 2026-05-15

Single-row-per-daemon liveness table. Each launchd LaunchAgent / Railway
service that runs a persistent process writes a heartbeat on a cadence
matched to the CLI ``--check`` probe's staleness threshold (60 minutes
today; heartbeats fire every 15 min so four attempts must miss before
the probe flips red).

Replaces the prior heartbeat-via-application_log approach for
``trade_monitor``: that probe queried ``MAX(recorded_at) WHERE
engine='trade_monitor'`` which goes red on quiet trading days because
the monitor only writes ``application_log`` rows on fills / reconnects.
A single-row UPSERT on this table is unambiguous: ``last_heartbeat``
advances on a schedule, ``status`` is the daemon's self-report.

Schema design — four daemon names are pre-seeded (one per launchd
agent) so the UPSERT writer always has a row to UPDATE. Other daemons
(`data_operations`, `engine_service`, `allocator`) are scheduled cron-
style today and don't need heartbeats — they get a single row each at
``status='healthy'`` so a future per-daemon heartbeat rollout can drop
in without a schema change.

Initial scope: ``trade_monitor`` is the only writer. The other three
rows are placeholders.

Columns:
    daemon_name TEXT PRIMARY KEY  — well-known label, e.g. 'trade_monitor'
    last_heartbeat TIMESTAMPTZ    — last UPSERT timestamp
    status TEXT                   — 'healthy' / 'degraded' / 'down'
    extra JSONB                   — optional metadata (e.g. ws_reconnects)
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260515_0000"
down_revision: str | None = "20260514_2500"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "daemon_heartbeats",
        sa.Column("daemon_name", sa.Text, primary_key=True),
        sa.Column(
            "last_heartbeat",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "status",
            sa.Text,
            nullable=False,
            server_default=sa.text("'healthy'"),
        ),
        sa.Column("extra", sa.dialects.postgresql.JSONB),
        schema="platform",
    )
    # Status must be one of the three known states so the probe's
    # interpretation stays bounded.
    op.create_check_constraint(
        "ck_daemon_heartbeats_status",
        "daemon_heartbeats",
        "status IN ('healthy', 'degraded', 'down')",
        schema="platform",
    )
    # Seed one row per known daemon. The status='healthy' default lets
    # the CLI probe report green from minute zero; trade_monitor will
    # immediately start advancing last_heartbeat on its 15-min cadence,
    # and the others (cron-style) keep their seeded timestamp as a
    # tombstone until they're given heartbeat writers.
    op.execute(
        """
        INSERT INTO platform.daemon_heartbeats (daemon_name, last_heartbeat, status)
        VALUES
            ('trade_monitor',  now(), 'healthy'),
            ('data_operations', now(), 'healthy'),
            ('engine_service',  now(), 'healthy'),
            ('allocator',       now(), 'healthy')
        ON CONFLICT (daemon_name) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_daemon_heartbeats_status",
        "daemon_heartbeats",
        schema="platform",
    )
    op.drop_table("daemon_heartbeats", schema="platform")
