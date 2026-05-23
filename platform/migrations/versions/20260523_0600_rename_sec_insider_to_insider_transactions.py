"""Phase 1.1 — rename sec_insider_transactions → insider_transactions + compat view + source column.

Per v2 plan §3.1 and v2 spec §10 (compatibility-view pattern).

Three things in this migration:

1. **Rename the table.** `platform.sec_insider_transactions` →
   `platform.insider_transactions` (the source-axis-neutral name; future
   non-SEC providers tag with `source='fmp'` or per-country adapters).

2. **Add `source` column with CHECK constraint.** `source TEXT NOT NULL`,
   CHECK `source IN ('sec','fmp')`. Backfilled with `'sec'` (current sole
   producer). Future per-country adapters (Task #15) extend the CHECK.

3. **Compatibility view at the OLD name.** `CREATE VIEW
   platform.sec_insider_transactions AS SELECT * FROM
   platform.insider_transactions WHERE source = 'sec'`.

   Per v2 spec §10.1: missed READ consumers (engines, dashboards) continue
   to work transparently. Missed WRITE consumers (INSERT/UPDATE/DELETE on
   the old name) fail LOUD with `ERROR: cannot insert into view`. View
   drops in Phase 5 after consumer migration verified.

Revision ID: 20260523_0600
Revises: 20260523_0500
Create Date: 2026-05-23
"""
import sqlalchemy as sa
from alembic import op

revision: str = "20260523_0600"
down_revision: str | None = "20260523_0500"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE platform.sec_insider_transactions RENAME TO insider_transactions")
    op.execute("ALTER INDEX IF EXISTS platform.sec_insider_transactions_dedupe_uk RENAME TO insider_transactions_dedupe_uk")
    op.execute("ALTER INDEX IF EXISTS platform.ix_sec_insider_transactions_filing_date RENAME TO ix_insider_transactions_filing_date")
    op.execute("ALTER INDEX IF EXISTS platform.ix_sec_insider_transactions_ticker_date RENAME TO ix_insider_transactions_ticker_date")

    op.add_column(
        "insider_transactions",
        sa.Column("source", sa.Text(), nullable=True, server_default="sec"),
        schema="platform",
    )
    op.execute("UPDATE platform.insider_transactions SET source = 'sec' WHERE source IS NULL")
    op.alter_column("insider_transactions", "source", nullable=False, schema="platform")
    op.create_check_constraint(
        "ck_insider_transactions_source",
        "insider_transactions",
        "source IN ('sec', 'fmp')",
        schema="platform",
    )

    op.execute(
        """
        CREATE VIEW platform.sec_insider_transactions AS
            SELECT * FROM platform.insider_transactions WHERE source = 'sec'
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS platform.sec_insider_transactions")
    op.drop_constraint("ck_insider_transactions_source", "insider_transactions", schema="platform")
    op.drop_column("insider_transactions", "source", schema="platform")
    op.execute("ALTER INDEX IF EXISTS platform.insider_transactions_dedupe_uk RENAME TO sec_insider_transactions_dedupe_uk")
    op.execute("ALTER INDEX IF EXISTS platform.ix_insider_transactions_filing_date RENAME TO ix_sec_insider_transactions_filing_date")
    op.execute("ALTER INDEX IF EXISTS platform.ix_insider_transactions_ticker_date RENAME TO ix_sec_insider_transactions_ticker_date")
    op.execute("ALTER TABLE platform.insider_transactions RENAME TO sec_insider_transactions")
