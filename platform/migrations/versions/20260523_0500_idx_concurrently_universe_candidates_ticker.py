"""Phase 0 — CREATE INDEX CONCURRENTLY on universe_candidates(ticker).

Per `docs/superpowers/audits/2026-05-23-referential-integrity-index-audit.md`:
`platform.universe_candidates` PK is `(as_of_date, engine, ticker)` —
ticker is NOT the leading PK column. Without a separate index on ticker,
the Phase 2 FK `(ticker) REFERENCES ticker_classifications(ticker)` would
cause full-table scans on parent DELETE (ON DELETE RESTRICT).

`CREATE INDEX CONCURRENTLY` requires `autocommit_block` because it cannot
run inside a transaction. Lock is `SHARE UPDATE EXCLUSIVE` — concurrent
reads + writes proceed during the build.

Revision ID: 20260523_0500
Revises: 20260522_0200
Create Date: 2026-05-23
"""
from alembic import op

revision: str = "20260523_0500"
down_revision: str | None = "20260522_0200"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_universe_candidates_ticker
            ON platform.universe_candidates (ticker)
        """)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("""
            DROP INDEX CONCURRENTLY IF EXISTS platform.idx_universe_candidates_ticker
        """)
