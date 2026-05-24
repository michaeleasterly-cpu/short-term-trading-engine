"""v2.2 ticker-reuse — partial UNIQUE index on active row + composite
lookup index on (ticker, lifetime_start, lifetime_end).

This is split from 20260524_1700 because `CREATE INDEX CONCURRENTLY`
cannot run inside an Alembic transaction. The autocommit_block context
manager temporarily exits the migration's transactional envelope.

After this migration:
  - `ON CONFLICT (ticker) WHERE lifetime_end IS NULL` clauses in
    producers resolve to `tc_ticker_active_uniq` for inference.
  - Date-aware lookups (used by any future code that wants to ask
    "which classification was active for ticker X on date D?") get
    the composite index for an index-only scan.

Revision ID: 20260524_1701
Revises: 20260524_1700
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_1701"
down_revision: str | None = "20260524_1700"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # Partial UNIQUE on (ticker) for the currently-active row.
        # Matches `ON CONFLICT (ticker) WHERE lifetime_end IS NULL`
        # producer clauses.
        op.execute(
            """
            CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS
                tc_ticker_active_uniq
            ON platform.ticker_classifications (ticker)
            WHERE lifetime_end IS NULL
            """
        )
        # Composite index for date-aware lookups
        # (ticker, lifetime_start, lifetime_end).
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                tc_ticker_lifetime
            ON platform.ticker_classifications (ticker, lifetime_start, lifetime_end)
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS platform.tc_ticker_lifetime"
        )
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS platform.tc_ticker_active_uniq"
        )
