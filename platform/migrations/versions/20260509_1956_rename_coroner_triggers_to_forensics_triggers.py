"""rename platform.coroner_triggers to platform.forensics_triggers

Revision ID: 20260509_1956
Revises: 20260509_0000
Create Date: 2026-05-09 19:56:58.452521

The Forensics service was previously named "Coroner". The application code
and master plan have moved on; this migration brings the schema into line.
The table contents (currently empty in all environments — it's a stub) and
column shape are preserved.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260509_1956"
down_revision: str | None = "20260509_0000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.rename_table("coroner_triggers", "forensics_triggers", schema="platform")


def downgrade() -> None:
    op.rename_table("forensics_triggers", "coroner_triggers", schema="platform")
