"""v2.2 P6 — prices_daily: FK NOT VALID + VALIDATE + classification_id index.

Step 3 of the split rollout (see 20260524_0700 for the 3-step rationale).

Pre-condition: the ops.py stage `prices_daily_backfill_classification_id` has
populated `classification_id` for every row that has a matching parent in
`ticker_classifications(current_ticker)`. The 335,159 orphan rows
(166 distinct orphan tickers; Path A per spec §1.11) remain with
classification_id IS NULL — those get filled by a follow-up parent_resolver run.

FK NOT VALID + VALIDATE works because NULL classification_id rows pass FK
checks by default (FK constraints only validate non-NULL values).

CREATE INDEX takes a SHARE lock on prices_daily during build. Estimated
5-15 minutes on 21M rows. SET LOCAL statement_timeout = '30min' headroom.

Disk impact: ~500-700 MB for the B-tree index on 21M rows.

Revision ID: 20260524_0701
Revises: 20260524_0700
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_0701"
down_revision: str | None = "20260524_0700"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # FK NOT VALID. Wrapped in DO $$ for partial-replay idempotency.
    op.execute(
        """
        DO $$
        BEGIN
            ALTER TABLE platform.prices_daily
                ADD CONSTRAINT prices_daily_classification_id_fk
                FOREIGN KEY (classification_id) REFERENCES platform.ticker_classifications(id)
                ON UPDATE CASCADE ON DELETE RESTRICT NOT VALID;
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END $$
        """
    )

    # VALIDATE under raised statement_timeout (asyncpg won't accept multi-statement
    # prepared statements; SET LOCAL in its own execute).
    op.execute("SET LOCAL statement_timeout = '30min'")
    op.execute(
        "ALTER TABLE platform.prices_daily VALIDATE CONSTRAINT prices_daily_classification_id_fk"
    )

    # Index on classification_id for join perf (Postgres doesn't auto-index FK columns).
    op.execute("SET LOCAL statement_timeout = '30min'")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS prices_daily_classification_id_idx
            ON platform.prices_daily (classification_id)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS platform.prices_daily_classification_id_idx")
    op.execute(
        """
        ALTER TABLE platform.prices_daily
            DROP CONSTRAINT IF EXISTS prices_daily_classification_id_fk
        """
    )
    # Column DROP stays in 20260524_0700 downgrade.
