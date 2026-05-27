"""Add b-tree index on platform.ticker_history(ticker, valid_from DESC)
to fix the classification_id-trigger Seq Scan that hangs daily_bars.

The 16 `tg_set_classification_id_<table>` BEFORE-INSERT triggers all
run this query against ticker_history per row:

    SELECT classification_id FROM platform.ticker_history
    WHERE ticker = NEW.ticker
      AND valid_from <= NEW.<asof>
      AND (valid_to IS NULL OR valid_to >= NEW.<asof>)
    ORDER BY valid_from DESC LIMIT 1

Without an index on (ticker, valid_from) the planner picks a Seq Scan
over all 13,840+ ticker_history rows — ~0.45s/row on the live DB.
On a daily_bars all-universe promote (~7,600 rows) that's ~57 min of
pure trigger work. On the larger fundamentals / earnings promote
batches the cost is unbounded.

Existing indexes don't cover the lookup:

* ticker_history_pkey (classification_id, valid_from) — wrong leading key
* ticker_history_no_overlap (GiST classification_id + range) — for overlap exclusion
* ticker_history_ticker_active_idx (ticker WHERE valid_to IS NULL) — partial, no historical rows

EXPLAIN ANALYZE on the AAPL/2026-05-26 lookup (2026-05-27 02:55 UTC):

    Limit  (cost=384.21..384.22 rows=1 width=19) (actual time=2.006..2.007 rows=1)
      ->  Sort
            ->  Seq Scan on ticker_history  (cost=0.00..384.20 rows=1 width=19)
                  Filter: ticker = 'AAPL' AND valid_from <= '2026-05-26' AND ...
                  Rows Removed by Filter: 13839

Plain CREATE INDEX (no CONCURRENTLY) is fine here: ticker_history has
13,840 rows — index build is sub-second, the brief ACCESS EXCLUSIVE
lock during build is acceptable. CONCURRENTLY would force per-statement
autocommit which our async Alembic runner doesn't easily expose.

Revision ID: 20260527_0300
Revises: 20260525_1200
Create Date: 2026-05-27
"""
from alembic import op

revision: str = "20260527_0300"
down_revision: str | None = "20260525_1200"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS "
        "ticker_history_ticker_valid_from_idx "
        "ON platform.ticker_history (ticker, valid_from DESC)"
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS "
        "platform.ticker_history_ticker_valid_from_idx"
    )
