"""prices_daily perf — date index + distinct-tickers matview

The operator dashboard's platform-health panel was taking ~120s cold
because three sub-queries each scanned the 20M-row ``platform.prices_daily``
table sequentially:

* ``MAX(date) WHERE date > now() - 10 days`` — no index on date alone
  (the existing ``(ticker, date)`` index leads with ticker, so a
  date-only predicate falls back to a seq scan).
* ``SELECT DISTINCT ticker FROM prices_daily`` (3 cross-reference
  checks + the coverage-gap check) — distinct on a 20M-row column is
  ~17s every time.

This migration:

1. Adds ``idx_prices_daily_date`` so date-bounded queries cheaply
   range-scan.
2. Creates ``platform.prices_daily_tickers`` materialized view: one row
   per ticker that has bars + most-recent date. ~7,700 rows; queries
   against the matview return in ~10ms.

The matview is refreshed at the end of every post-close (see
``scripts/run_post_close.sh``). The refresh is CONCURRENT so reads keep
serving stale-but-correct data while it runs (≈1s on the current row
count).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260514_1800"
down_revision: str | None = "20260514_0000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Date-only index for "MAX(date) WHERE date > X" and similar
    # range predicates. Existing (ticker, date) leads with ticker,
    # which doesn't help date-only filters.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_prices_daily_date "
        "ON platform.prices_daily(date)"
    )

    # Materialized view of distinct tickers with their most-recent bar
    # date. Used by the dashboard's coverage + cross-reference checks
    # so they don't repeatedly distinct-scan 20M rows.
    op.execute(
        """
        CREATE MATERIALIZED VIEW IF NOT EXISTS platform.prices_daily_tickers AS
        SELECT ticker, MAX(date) AS latest_date, COUNT(*)::bigint AS row_count
        FROM platform.prices_daily
        GROUP BY ticker
        """
    )
    # Unique index so REFRESH MATERIALIZED VIEW CONCURRENTLY works
    # (Postgres requires a unique index on the matview).
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_prices_daily_tickers_ticker "
        "ON platform.prices_daily_tickers(ticker)"
    )


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS platform.prices_daily_tickers")
    op.execute("DROP INDEX IF EXISTS platform.idx_prices_daily_date")
