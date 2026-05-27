"""Add a partial index on platform.prices_daily(date, ticker) WHERE
delisted=false to speed up the daily_bars active-universe query.

The handler computes the active universe via:

    SELECT DISTINCT ticker FROM platform.prices_daily
    WHERE date >= CURRENT_DATE - INTERVAL '90 days' AND delisted = false
    ORDER BY ticker

Pre-index: 76 seconds (Parallel Index Scan on idx_prices_daily_date over
~457k rows, then HashAggregate + Sort). Run on every data_ops cron.

The new partial index (date, ticker) WHERE delisted = false is small
(~7,600 tickers × ~60 sessions = ~460k entries; index size ~10MB), and
supports an Index-Only Scan with skip-scan deduplication. Expected
sub-second query time.

Plain CREATE INDEX (no CONCURRENTLY) — index build on a 21M-row table
takes ~30-60 seconds and briefly locks the table. Acceptable for this
emergency-ops fix; CONCURRENTLY's per-statement-autocommit requirement
isn't easily expressed via async Alembic.

Revision ID: 20260527_0400
Revises: 20260527_0300
Create Date: 2026-05-27
"""
from alembic import op

revision: str = "20260527_0400"
down_revision: str | None = "20260527_0300"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS prices_daily_active_universe_idx "
        "ON platform.prices_daily (date, ticker) "
        "WHERE delisted = false"
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS platform.prices_daily_active_universe_idx"
    )
