"""v2.2 P6 — prices_daily: ADD COLUMN classification_id (DDL-only; backfill follows).

The largest child table — 21,331,836 rows, 4.3 GB current footprint.

**SPLIT into 3 steps after the 2026-05-23 incident** where a single-transaction
UPDATE on 21M rows generated 1.95 GB WAL → triggered Supabase auto-protective
read-only mode → 4-hour disk-resize cooldown. Per
`feedback_run_gates_locally_on_commit` + post-incident memory, the new sequence is:

  Step 1 (this migration, 20260524_0700) — ADD COLUMN only. Fast.
  Step 2 (ops.py stage `prices_daily_backfill_classification_id`) — chunked UPDATE
          in 100K-row transactions with COMMIT + sleep between chunks. WAL recycles
          incrementally; no single-transaction blowup.
  Step 3 (migration 20260524_0701) — FK NOT VALID + VALIDATE + index AFTER backfill.

This file is now ONLY step 1. Backfill + FK live elsewhere.

Disk impact of step 1 alone: 21M × char(14) = ~294 MB column data; no index yet.
Step 1 is fast (PG ≥11 allows ADD COLUMN with no DEFAULT to skip table rewrite).

Revision ID: 20260524_0700
Revises: 20260524_0600
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_0700"
down_revision: str | None = "20260524_0600"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # Step 1: add the column only. No UPDATE, no FK, no index — those live in
    # the ops.py stage + 20260524_0701 migration respectively.
    op.execute(
        "ALTER TABLE platform.prices_daily ADD COLUMN IF NOT EXISTS classification_id text"
    )


def downgrade() -> None:
    # Defensive: also drop the FK + index that 20260524_0701 adds, if present —
    # so this migration's downgrade leaves the table in its pre-P6 state regardless
    # of which 0700-series revisions are applied.
    op.execute("DROP INDEX IF EXISTS platform.prices_daily_classification_id_idx")
    op.execute(
        """
        ALTER TABLE platform.prices_daily
            DROP CONSTRAINT IF EXISTS prices_daily_classification_id_fk
        """
    )
    op.execute(
        "ALTER TABLE platform.prices_daily DROP COLUMN IF EXISTS classification_id"
    )
