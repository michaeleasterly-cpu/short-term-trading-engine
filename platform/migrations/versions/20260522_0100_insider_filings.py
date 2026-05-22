"""insider_filings — daily-granularity Form-4 insider transactions from FMP.

Carver-driven 2026-05-22: the vector engine candidate
``vector_beat_reversal_insider_filter_v1`` needs a 30-day-rolling MSPR
(Monthly Share Purchase Ratio) signal at DAILY resolution. The existing
``platform.insider_sentiment`` (Finnhub free-tier) carries monthly
aggregates only — information loss vs. the per-filing source — and is
empty pre-2025 at the operator's free Finnhub tier. After all three
vector filters this yields <4 testable trades, an order of magnitude
below the Lab credibility floor.

FMP's $200/yr Starter tier exposes ``/stable/insider-trading/search``
(per-symbol, paginated, ~12 pages of 100 rows = 2018→present for a
typical T1/T2 ticker; ~50 pages goes back to 2005 for AAPL-class names).
Every row is a Form-4 transaction line — exactly the substrate needed
to compute any rolling window (30d, 60d, 90d) downstream WITHOUT a
re-pull. This is a NEW SIBLING table, NOT a breaking change to the
existing monthly ``insider_sentiment`` table — the Finnhub adapter
keeps writing there for any consumer still bound to monthly MSPR.

Two ops stages drive this table:

* ``historical_insider_sentiment_daily`` — one-shot operator backfill.
  Run once after PR merge to populate 2018-01-01 → today for the full
  T1+T2 stock universe + delisted tickers in prices_daily.
* ``daily_insider_sentiment_delta`` — nightly incremental, in
  OPS_UPDATE_STAGES + a FeedProfile entry so the existing feed
  dispatcher schedules it like every other source-of-truth feed.

Dedupe key (PK): ``(symbol, transaction_date, reporting_cik,
transaction_type, securities_transacted, price)`` — every observable
field FMP returns combined. ON CONFLICT DO NOTHING makes both stages
idempotent under re-runs.

Index ``ix_insider_filings_symbol_txdate`` services the engine query
pattern ("rows for SYMBOL where transaction_date BETWEEN today-30 AND
today") with a single B-tree seek; no other access pattern is in flight.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260522_0100"
down_revision: str | None = "20260522_0000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "insider_filings",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("filing_date", sa.Date(), nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("reporting_cik", sa.Text(), nullable=False),
        sa.Column("company_cik", sa.Text(), nullable=True),
        # FMP transactionType is a 1-letter code joined to a label
        # ("S-Sale", "P-Purchase", "A-Award", "M-Exempt", "F-InKind",
        # etc.) — stored as-is for downstream filtering by code.
        sa.Column("transaction_type", sa.Text(), nullable=False),
        sa.Column("reporting_name", sa.Text(), nullable=True),
        sa.Column("type_of_owner", sa.Text(), nullable=True),
        # 'A' = Acquired, 'D' = Disposed. The canonical MSPR formula
        # uses this directly: MSPR = 100 * (sum(A.shares*price) -
        # sum(D.shares*price)) / (sum(A.shares*price) +
        # sum(D.shares*price)).
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


def downgrade() -> None:
    op.drop_index(
        "ix_insider_filings_txdate",
        table_name="insider_filings",
        schema="platform",
    )
    op.drop_index(
        "ix_insider_filings_symbol_txdate",
        table_name="insider_filings",
        schema="platform",
    )
    op.drop_table("insider_filings", schema="platform")
