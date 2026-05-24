"""Add classification_id + BEFORE INSERT trigger to platform.aar_events.

Engine-abstraction session (2026-05-24 handoff) needs this to unblock
their AAR-write conversion PRs. Same pattern as the 14 P7 triggers
in `20260524_1500` + the ORDER BY fix in `20260524_1901`.

`aar_events` is the After-Action Report substrate written by each
engine when a position closes. Today the row is identified by
(engine, trade_id, ticker); after this migration it also carries
the resolved `classification_id` so cross-engine analytics can join
on the stable identity dimension rather than the ticker string.

The trigger function uses `NEW.recorded_at::date` as the as-of date
for the `ticker_history` lookup. This is best-effort: the actual
trade close date might be inside `aar_data` JSONB, but the trigger
can't easily reach into JSONB. If the engine writer wants more
precision it can populate `classification_id` explicitly (the
trigger no-ops when the column is already set).

No FK in this migration: the new column is NULLABLE and unconstrained
so the trigger can leave it NULL when a row arrives for a ticker with
no `ticker_history` match (Path-A nullable contract, same as the 14
other tables). A future PR can add `ADD CONSTRAINT ... NOT VALID +
VALIDATE` once the residual nulls in the orphan tail are closed
(see defect #3 in the engine-session handoff).

Revision ID: 20260524_1903
Revises: 20260524_1902
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_1903"
down_revision: str | None = "20260524_1902"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # 1. Add nullable classification_id column.
    op.execute(
        """
        ALTER TABLE platform.aar_events
            ADD COLUMN IF NOT EXISTS classification_id TEXT
        """
    )
    # 2. Index for join performance.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_aar_events_classification_id
            ON platform.aar_events (classification_id)
            WHERE classification_id IS NOT NULL
        """
    )
    # 3. Trigger function — looks up ticker_history with the same
    #    ORDER BY pattern as 20260524_1901.
    op.execute("DROP TRIGGER IF EXISTS tg_aar_events_classification_id ON platform.aar_events")
    op.execute("DROP FUNCTION IF EXISTS platform.tg_set_classification_id_aar_events()")
    op.execute(
        """
        CREATE FUNCTION platform.tg_set_classification_id_aar_events()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.classification_id IS NULL THEN
                SELECT classification_id INTO NEW.classification_id
                FROM platform.ticker_history
                WHERE ticker = NEW.ticker
                  AND valid_from <= NEW.recorded_at::date
                  AND (valid_to IS NULL OR valid_to >= NEW.recorded_at::date)
                ORDER BY valid_from DESC
                LIMIT 1;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER tg_aar_events_classification_id
        BEFORE INSERT ON platform.aar_events
        FOR EACH ROW
        EXECUTE FUNCTION platform.tg_set_classification_id_aar_events()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS tg_aar_events_classification_id ON platform.aar_events")
    op.execute("DROP FUNCTION IF EXISTS platform.tg_set_classification_id_aar_events()")
    op.execute("DROP INDEX IF EXISTS platform.ix_aar_events_classification_id")
    op.execute("ALTER TABLE platform.aar_events DROP COLUMN IF EXISTS classification_id")
