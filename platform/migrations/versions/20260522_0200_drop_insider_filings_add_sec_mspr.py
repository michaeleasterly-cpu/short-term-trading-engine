"""drop platform.insider_filings (FMP), add insider_mspr_daily view over SEC.

Deprecate-forward of the 20260522_0100 ``insider_filings`` table that
PR #296 shipped. Operator directive 2026-05-22: the FMP per-filing
table duplicates ``platform.sec_insider_transactions`` (the SEC Form-4
ETL) at a smaller universe (FMP Starter ≈ 247 tickers vs SEC ≈ 1,306
distinct tickers / 646,881 rows / 1,499 T1+T2 STOCK tickers eligible).
The SEC table is the authoritative substrate; the FMP sibling was a
mistaken Carver instinct and never delivered new information.

Two changes here, both forward-only (the previous migration's
``downgrade()`` body is preserved as the inverse, so a manual rollback
to the pre-20260522_0100 schema is still expressible via Alembic):

1. DROP ``platform.insider_filings`` + its two indexes. Any rows in
   the table at the moment this migration runs are stale per-filing
   FMP rows whose information is already captured at higher coverage
   in ``platform.sec_insider_transactions``.

2. CREATE ``platform.insider_mspr_daily`` VIEW. The vector engine
   candidate ``vector_beat_reversal_insider_filter_v1`` consumes a
   30-day-rolling MSPR (Monthly Share Purchase Ratio) signal at daily
   resolution. The formula is

       MSPR = 100 * (BUY_VAL - SELL_VAL) / (BUY_VAL + SELL_VAL)

   where ``BUY_VAL = SUM(value) WHERE transaction_type = 'BUY'`` and
   ``SELL_VAL = SUM(value) WHERE transaction_type = 'SELL'``. Aggregation
   key is ``(ticker, filing_date)`` — one MSPR per ticker per filing
   day — which is the substrate the engine's rolling window queries.
   The SEC ETL normalises Form-4 transaction codes to the two strings
   ``'BUY'`` (P-Purchase, A-Award) and ``'SELL'`` (S-Sale, D-Disposition,
   F-InKind) at ingest time — verified live 2026-05-22: only those
   two values exist in ``sec_insider_transactions.transaction_type``.
   The view is REGULAR (not materialised) because the underlying
   ``sec_insider_transactions`` table is append-mostly + < 1M rows;
   a regular view re-evaluates in milliseconds and avoids a refresh
   contract.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260522_0200"
down_revision: str | None = "20260522_0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INSIDER_MSPR_VIEW_SQL = """
    CREATE OR REPLACE VIEW platform.insider_mspr_daily AS
    SELECT
        ticker,
        filing_date,
        SUM(CASE WHEN transaction_type = 'BUY' THEN value ELSE 0 END) AS buy_value,
        SUM(CASE WHEN transaction_type = 'SELL' THEN value ELSE 0 END) AS sell_value,
        CASE
            WHEN SUM(ABS(value)) = 0 THEN NULL
            ELSE 100.0 * (
                SUM(CASE WHEN transaction_type = 'BUY' THEN value ELSE 0 END)
                - SUM(CASE WHEN transaction_type = 'SELL' THEN value ELSE 0 END)
            ) / SUM(ABS(value))
        END AS mspr,
        COUNT(*) AS n_transactions
    FROM platform.sec_insider_transactions
    WHERE transaction_type IN ('BUY', 'SELL')
    GROUP BY ticker, filing_date
"""


def upgrade() -> None:
    # 1. DROP the FMP insider_filings table + indexes. Idempotent at
    # the table level (IF EXISTS) so a re-run on a fresh DB that
    # never had the table is a no-op rather than a hard fail.
    op.execute(
        "DROP INDEX IF EXISTS platform.ix_insider_filings_txdate"
    )
    op.execute(
        "DROP INDEX IF EXISTS platform.ix_insider_filings_symbol_txdate"
    )
    op.execute("DROP TABLE IF EXISTS platform.insider_filings CASCADE")

    # 2. CREATE the SEC-backed MSPR view.
    op.execute(_INSIDER_MSPR_VIEW_SQL)


def downgrade() -> None:
    # Drop the view; recreate the table per the original schema.
    op.execute("DROP VIEW IF EXISTS platform.insider_mspr_daily")

    op.create_table(
        "insider_filings",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("filing_date", sa.Date(), nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("reporting_cik", sa.Text(), nullable=False),
        sa.Column("company_cik", sa.Text(), nullable=True),
        sa.Column("transaction_type", sa.Text(), nullable=False),
        sa.Column("reporting_name", sa.Text(), nullable=True),
        sa.Column("type_of_owner", sa.Text(), nullable=True),
        sa.Column("acquisition_or_disposition", sa.Text(), nullable=True),
        sa.Column("direct_or_indirect", sa.Text(), nullable=True),
        sa.Column("form_type", sa.Text(), nullable=True),
        sa.Column("securities_transacted", sa.Numeric(20, 4), nullable=False),
        sa.Column("price", sa.Numeric(18, 4), nullable=False),
        sa.Column("securities_owned", sa.Numeric(20, 4), nullable=True),
        sa.Column("security_name", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "symbol",
            "transaction_date",
            "reporting_cik",
            "transaction_type",
            "securities_transacted",
            "price",
            name="insider_filings_pk",
        ),
        sa.CheckConstraint(
            "length(symbol) > 0", name="insider_filings_symbol_chk",
        ),
        sa.CheckConstraint(
            "securities_transacted >= 0",
            name="insider_filings_shares_chk",
        ),
        sa.CheckConstraint(
            "price >= 0", name="insider_filings_price_chk",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_insider_filings_symbol_txdate",
        "insider_filings",
        ["symbol", "transaction_date"],
        schema="platform",
    )
    op.create_index(
        "ix_insider_filings_txdate",
        "insider_filings",
        ["transaction_date"],
        schema="platform",
    )
