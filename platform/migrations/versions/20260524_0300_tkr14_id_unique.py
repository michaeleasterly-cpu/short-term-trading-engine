"""v2.2 P5 completion — flip ticker_classifications.id to NOT NULL + UNIQUE.

Pre-condition: P5 backfill complete (100% of rows have a TKR-14 id).
Verified live: 13,674/13,674 rows have id IS NOT NULL.

This migration enables FK references from child tables (P6) — FK targets
require either PK or UNIQUE on the referenced column. We add UNIQUE here
(NOT the PK swap; that's a later migration once every child table has
been migrated off ticker-keyed FKs).

NOT NULL + UNIQUE adds a B-tree index of ~500 KB (13K char(14) values).
Negligible disk impact per operator's space-cognizance note.

Pre-flight gate: `SELECT count(*) FROM ticker_classifications WHERE id IS NULL` = 0.
The ALTER COLUMN SET NOT NULL will fail loudly if any row has NULL id —
correct behavior; the backfill must be 100% before this migration runs.

Revision ID: 20260524_0300
Revises: 20260524_0200
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_0300"
down_revision: str | None = "20260524_0200"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # Pre-flight: every row must have an id. Raises if any row has NULL.
    op.execute(
        """
        DO $$
        DECLARE n bigint;
        BEGIN
            SELECT count(*) INTO n FROM platform.ticker_classifications WHERE id IS NULL;
            IF n > 0 THEN
                RAISE EXCEPTION 'ticker_classifications has % rows with id IS NULL — run ops.py --stage tkr14_backfill --param mode=mint --param dry_run=false first', n;
            END IF;
        END $$
        """
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications ALTER COLUMN id SET NOT NULL"
    )
    op.execute(
        """
        ALTER TABLE platform.ticker_classifications
            ADD CONSTRAINT ticker_classifications_id_uniq UNIQUE (id)
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE platform.ticker_classifications DROP CONSTRAINT IF EXISTS ticker_classifications_id_uniq"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications ALTER COLUMN id DROP NOT NULL"
    )
