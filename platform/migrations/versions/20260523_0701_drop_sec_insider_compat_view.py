"""Phase 2.5 — drop the platform.sec_insider_transactions compatibility view.

Operator directive 2026-05-23: "we will have to update all the engines
with the new database design so i say drop the view". The Phase 1
compatibility view served the rename's read-consumer transition but the
operator chose to force the engine updates now rather than retain the
view through Phase 5.

This migration drops the view AFTER all in-tpcore SQL queries have been
updated to query `platform.insider_transactions` directly (same-PR
producer-code changes in `tpcore/ingestion/handlers.py`,
`tpcore/quality/validation/checks/sec_insider_monotone.py`,
`tpcore/quality/validation/checks/sec_filings_freshness.py`).

LOGICAL feed name `sec_insider_transactions` is preserved in:
- tpcore/feeds/profile.py (FeedProfile key)
- tpcore/feeds/dispatcher.py (stage mapping)
- tpcore/engine_profile.py (data_dependencies frozenset)
- tpcore/providers.py (provider mapping)
These remain unchanged — they're string identifiers, not physical
table references. A logical-feed-name rename is deferred to a later
phase.

Downgrade re-creates the view exactly as Phase 1 Migration 1 did.

Revision ID: 20260523_0701
Revises: 20260523_0700
Create Date: 2026-05-23
"""
from alembic import op

revision: str = "20260523_0701"
down_revision: str | None = "20260523_0700"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS platform.sec_insider_transactions")


def downgrade() -> None:
    op.execute(
        """
        CREATE VIEW platform.sec_insider_transactions AS
            SELECT * FROM platform.insider_transactions WHERE source = 'sec'
        """
    )
