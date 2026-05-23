"""v2.2 P6 — universe_candidates: add classification_id + FK to ticker_classifications.

First child table in the P6 rollout (smallest orphan count: 1). Per spec
§1.11 universe_candidates is Path B (DELETE) for orphan cleanup —
engine output, rebuildable on next engine run.

Migration sequence (all in one transaction, all idempotent):
1. ADD COLUMN classification_id text NULL.
2. UPDATE backfill via JOIN on ticker_classifications.current_ticker
   for active-status rows.
3. DELETE rows where backfill produced NULL (the orphans). Per spec
   §1.11 these are stale engine outputs that don't survive the cleanup
   — rebuilt on next engine run.
4. ALTER COLUMN classification_id SET NOT NULL (now safe).
5. ADD CONSTRAINT FK NOT VALID referencing ticker_classifications(id).
   ON UPDATE CASCADE: if a TKR-14 id ever changes (rare; not expected),
   propagate. ON DELETE RESTRICT: protect — never let a parent classification
   be deleted while children reference it.
6. VALIDATE CONSTRAINT (under SET LOCAL statement_timeout = '30min' per
   operator 2026-05-23 directive — no Supabase dashboard raise needed for
   small tables; the inline SET handles it).

Pre-flight gates:
- 20260524_0300 applied (ticker_classifications.id is NOT NULL + UNIQUE).
- universe_candidates is small (~4592 rows); VALIDATE completes in <1s.

This is the FIRST test of the P6 pattern. Once green, the same pattern
applies to the other 13 child tables, in ascending orphan-count order
per v2.2 plan §3 P6.

Revision ID: 20260524_0400
Revises: 20260524_0300
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_0400"
down_revision: str | None = "20260524_0300"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # Step 1: add the column (nullable for the backfill window).
    op.execute(
        "ALTER TABLE platform.universe_candidates ADD COLUMN IF NOT EXISTS classification_id text"
    )

    # Step 2: backfill from the parent table, joining on current_ticker for
    # active-status classification rows.
    op.execute(
        """
        UPDATE platform.universe_candidates uc
        SET classification_id = tc.id
        FROM platform.ticker_classifications tc
        WHERE uc.ticker = tc.current_ticker
          AND tc.status IN ('active', 'active_when_issued')
          AND uc.classification_id IS NULL
        """
    )

    # Step 3: per spec §1.11 Path B — DELETE orphan rows (engine output,
    # rebuildable on next engine run).
    op.execute(
        """
        DELETE FROM platform.universe_candidates WHERE classification_id IS NULL
        """
    )

    # Step 4: NOT NULL flip — safe now that orphans are deleted.
    op.execute(
        "ALTER TABLE platform.universe_candidates ALTER COLUMN classification_id SET NOT NULL"
    )

    # Step 5 + 6: FK NOT VALID then VALIDATE. Wrapped in DO $$ ... EXCEPTION
    # WHEN duplicate_object for partial-replay idempotency.
    op.execute(
        """
        DO $$
        BEGIN
            ALTER TABLE platform.universe_candidates
                ADD CONSTRAINT universe_candidates_classification_id_fk
                FOREIGN KEY (classification_id) REFERENCES platform.ticker_classifications(id)
                ON UPDATE CASCADE ON DELETE RESTRICT NOT VALID;
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END $$
        """
    )
    # SET LOCAL + the ALTER must be separate op.execute calls — asyncpg
    # raises 'cannot insert multiple commands into a prepared statement'
    # when both are in one string. Alembic transactional DDL ensures they
    # run in the same migration transaction so SET LOCAL stays in scope.
    op.execute("SET LOCAL statement_timeout = '30min'")
    op.execute(
        """
        ALTER TABLE platform.universe_candidates
            VALIDATE CONSTRAINT universe_candidates_classification_id_fk
        """
    )

    # Index on the FK column — required for join performance (Postgres does
    # NOT auto-index FK columns). Concurrent build to avoid table lock.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS universe_candidates_classification_id_idx
            ON platform.universe_candidates (classification_id)
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS platform.universe_candidates_classification_id_idx"
    )
    op.execute(
        """
        ALTER TABLE platform.universe_candidates
            DROP CONSTRAINT IF EXISTS universe_candidates_classification_id_fk
        """
    )
    op.execute(
        "ALTER TABLE platform.universe_candidates DROP COLUMN IF EXISTS classification_id"
    )
    # Note: deliberately do NOT restore the deleted orphan rows; they were
    # stale engine outputs per spec §1.11 and the engine rebuilds them on
    # next run anyway.
