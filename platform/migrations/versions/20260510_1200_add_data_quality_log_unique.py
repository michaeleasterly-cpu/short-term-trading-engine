"""add unique(source, timestamp) on data_quality_log

Revision ID: 20260510_1200
Revises: 20260510_0049
Create Date: 2026-05-10

The Data Validation Suite (`tpcore.quality.validation`) writes one row per
check per run to `platform.data_quality_log`. We need a unique constraint
so the writer can use the existing D-137 Pattern A (`ON CONFLICT DO
NOTHING`) for idempotency — re-running a suite execution should not
double-insert.

`(source, timestamp)` is the natural key: each run uses one shared
`SuiteResult.started_at` and three distinct `source` values
(`validation.delistings`, `validation.constituent`, `validation.splits`).
"""
from __future__ import annotations

from typing import Sequence

from alembic import op

revision: str = "20260510_1200"
down_revision: str | None = "20260510_0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_dq_source_ts",
        "data_quality_log",
        ["source", "timestamp"],
        schema="platform",
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_dq_source_ts",
        "data_quality_log",
        schema="platform",
        type_="unique",
    )
