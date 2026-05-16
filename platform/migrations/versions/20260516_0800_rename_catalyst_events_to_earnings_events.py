"""rename platform.catalyst_events → platform.earnings_events

The table only ever held ``EARNINGS_BEAT`` rows (verified 2026-05-16:
13,848 rows / 1,104 tickers, 100% source=fmp) — "catalyst" was an
aspirational misnomer for a general-catalyst engine that does not
exist. Rename table + its index + PK constraint in lockstep with the
stage / validation check / selfheal HealSpec / Vector data-loading.

Pure rename — NO data is dropped or rewritten. Fully idempotent: each
step is guarded so a re-run (or a fresh DB that built the table under
the new name via a squashed history) is a clean no-op.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260516_0800"
down_revision: str | None = "20260516_0700"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'platform'
                  AND table_name = 'catalyst_events'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'platform'
                  AND table_name = 'earnings_events'
            ) THEN
                ALTER TABLE platform.catalyst_events
                    RENAME TO earnings_events;
            END IF;

            IF EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE schemaname = 'platform'
                  AND indexname = 'ix_catalyst_events_ticker_date'
            ) THEN
                ALTER INDEX platform.ix_catalyst_events_ticker_date
                    RENAME TO ix_earnings_events_ticker_date;
            END IF;

            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'pk_catalyst_events'
            ) THEN
                ALTER TABLE platform.earnings_events
                    RENAME CONSTRAINT pk_catalyst_events
                    TO pk_earnings_events;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'platform'
                  AND table_name = 'earnings_events'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'platform'
                  AND table_name = 'catalyst_events'
            ) THEN
                ALTER TABLE platform.earnings_events
                    RENAME TO catalyst_events;
            END IF;

            IF EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE schemaname = 'platform'
                  AND indexname = 'ix_earnings_events_ticker_date'
            ) THEN
                ALTER INDEX platform.ix_earnings_events_ticker_date
                    RENAME TO ix_catalyst_events_ticker_date;
            END IF;

            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'pk_earnings_events'
            ) THEN
                ALTER TABLE platform.catalyst_events
                    RENAME CONSTRAINT pk_earnings_events
                    TO pk_catalyst_events;
            END IF;
        END $$;
        """
    )
