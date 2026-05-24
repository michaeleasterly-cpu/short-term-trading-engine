"""v2.2 ticker-reuse — add lifetime_start/lifetime_end to ticker_classifications
and drop the legacy UNIQUE(ticker) constraint.

Motivation: a ticker (e.g. "FB") can legitimately be reused by a
different entity after the original is renamed / delisted. The legacy
`UNIQUE(ticker)` forced one classification per ticker — any reuse would
overwrite the prior entity's row. With lifetime columns + a partial
UNIQUE on the active row, we get:

  - One CURRENTLY-ACTIVE row per ticker (lifetime_end IS NULL)
  - Multiple historical rows per ticker (each with valid lifetime_end)
  - Producer UPSERTs target the active row via the partial-index
    inference clause: ``ON CONFLICT (ticker) WHERE lifetime_end IS NULL``

Note on triggers: the 14 BEFORE INSERT triggers added in 20260524_1500
look up `ticker_history` (which already has its own SCD-2 lifetime),
NOT `ticker_classifications` directly. So the triggers are unaffected
by this change — date-aware ticker→classification resolution already
flows through `ticker_history.valid_from / valid_to`. The lifetime
columns added here are for producer-UPSERT uniqueness only.

Note on `issuer_securities`: that table is the issuer-to-classification
SCD-2 dimension; this migration adds the TICKER-STRING SCD-2 dimension
to ticker_classifications. The two are orthogonal — a single
classification_id can have its issuer change over time (issuer_securities)
AND its ticker string lifetime can close (the lifetime cols here).

The partial UNIQUE index is created in a SEPARATE follow-up migration
(20260524_1701) because PostgreSQL `CREATE INDEX CONCURRENTLY` cannot
run inside an Alembic transaction. Here we DROP the old UNIQUE constraint
and add the lifetime columns; the next migration adds the new index.
This is safe because nothing INSERTs into ticker_classifications
between these two migrations in practice.

Revision ID: 20260524_1700
Revises: 20260524_1600
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_1700"
down_revision: str | None = "20260524_1600"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # 1. Add lifetime columns with safe defaults — all existing 7,600
    #    rows get (1900-01-01, NULL) meaning "active since beginning of
    #    time, still active". Fast on this row count.
    op.execute(
        """
        ALTER TABLE platform.ticker_classifications
            ADD COLUMN IF NOT EXISTS lifetime_start DATE NOT NULL DEFAULT '1900-01-01',
            ADD COLUMN IF NOT EXISTS lifetime_end   DATE NULL
        """
    )

    # 2. CHECK constraint — lifetime_end (when set) must strictly
    #    follow lifetime_start. Idempotent.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'tc_lifetime_order'
                  AND conrelid = 'platform.ticker_classifications'::regclass
            ) THEN
                ALTER TABLE platform.ticker_classifications
                ADD CONSTRAINT tc_lifetime_order
                CHECK (lifetime_end IS NULL OR lifetime_end > lifetime_start);
            END IF;
        END$$;
        """
    )

    # 3. Drop the legacy UNIQUE(ticker) — superseded by the partial
    #    UNIQUE on (ticker) WHERE lifetime_end IS NULL in the next
    #    migration.
    op.execute(
        """
        ALTER TABLE platform.ticker_classifications
            DROP CONSTRAINT IF EXISTS ticker_classifications_ticker_key
        """
    )


def downgrade() -> None:
    # Reverse: re-add UNIQUE(ticker) (will fail if any reused tickers
    # exist — that's intentional, downgrade past a real reuse event
    # would destroy data). Then drop the lifetime columns.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM platform.ticker_classifications
                WHERE lifetime_end IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'cannot downgrade: ticker_classifications has %s rows with lifetime_end set — re-adding UNIQUE(ticker) would silently keep only the active per-ticker row. Resolve the reused tickers manually before downgrading.',
                    (SELECT count(*) FROM platform.ticker_classifications WHERE lifetime_end IS NOT NULL);
            END IF;
        END$$;
        """
    )
    op.execute(
        """
        ALTER TABLE platform.ticker_classifications
            ADD CONSTRAINT ticker_classifications_ticker_key UNIQUE (ticker)
        """
    )
    op.execute(
        """
        ALTER TABLE platform.ticker_classifications
            DROP CONSTRAINT IF EXISTS tc_lifetime_order,
            DROP COLUMN IF EXISTS lifetime_end,
            DROP COLUMN IF EXISTS lifetime_start
        """
    )
