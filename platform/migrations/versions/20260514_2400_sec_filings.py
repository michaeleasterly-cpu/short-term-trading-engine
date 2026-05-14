"""sec_insider_transactions + sec_material_events — SEC EDGAR ingest tables.

Phase 2 of the standardized data-adapter pipeline (2026-05-14).
SEC EDGAR is the reference implementation that establishes the 5-stage
contract (docs/superpowers/pipelines/data_adapter_pipeline.md).

Two tables:

1. ``sec_insider_transactions`` — one row per Form 4 transaction line
   (each Form 4 filing can carry multiple non-derivative transactions).
   ``transaction_type`` is the platform's canonical BUY/SELL bucket
   derived from Form 4's acquired-disposed code (A→BUY, D→SELL).

2. ``sec_material_events`` — one row per 8-K item-number. 8-K filings
   commonly carry multiple item codes (e.g., 2.02 + 9.01 — earnings +
   exhibits). One row per item keeps the dedupe key simple and the
   downstream filter (engine-specific item allow-lists) cheap.

Physical-truth predicates baked into CHECK constraints so the
validation suite's freshness check can assume the table is internally
consistent.

CIK→ticker mapping is held in memory by the adapter (re-fetched per
run from sec.gov/files/company_tickers.json — 8k tickers, ~1 MB
payload). No platform table needed for that.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260514_2400"
down_revision: str | None = "20260514_2300"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sec_insider_transactions",
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("filing_date", sa.Date(), nullable=False),
        sa.Column("insider_name", sa.Text(), nullable=False),
        sa.Column("transaction_type", sa.Text(), nullable=False),
        sa.Column("shares", sa.BigInteger(), nullable=False),
        sa.Column("price", sa.Numeric(18, 4), nullable=False),
        sa.Column("value", sa.Numeric(20, 2), nullable=False),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "ticker", "filing_date", "insider_name", "transaction_type", "shares",
            name="sec_insider_transactions_dedupe_uk",
        ),
        sa.CheckConstraint(
            "transaction_type IN ('BUY', 'SELL')",
            name="sec_insider_transactions_type_chk",
        ),
        sa.CheckConstraint("shares > 0", name="sec_insider_transactions_shares_chk"),
        sa.CheckConstraint("price >= 0", name="sec_insider_transactions_price_chk"),
        sa.CheckConstraint("value >= 0", name="sec_insider_transactions_value_chk"),
        schema="platform",
    )
    op.create_index(
        "ix_sec_insider_transactions_ticker_date",
        "sec_insider_transactions",
        ["ticker", "filing_date"],
        schema="platform",
    )
    op.create_index(
        "ix_sec_insider_transactions_filing_date",
        "sec_insider_transactions",
        ["filing_date"],
        schema="platform",
    )

    op.create_table(
        "sec_material_events",
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("filing_date", sa.Date(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "ticker", "filing_date", "event_type",
            name="sec_material_events_dedupe_uk",
        ),
        sa.CheckConstraint(
            "length(event_type) > 0",
            name="sec_material_events_type_nonempty_chk",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_sec_material_events_ticker_date",
        "sec_material_events",
        ["ticker", "filing_date"],
        schema="platform",
    )
    op.create_index(
        "ix_sec_material_events_filing_date",
        "sec_material_events",
        ["filing_date"],
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sec_material_events_filing_date",
        table_name="sec_material_events",
        schema="platform",
    )
    op.drop_index(
        "ix_sec_material_events_ticker_date",
        table_name="sec_material_events",
        schema="platform",
    )
    op.drop_table("sec_material_events", schema="platform")
    op.drop_index(
        "ix_sec_insider_transactions_filing_date",
        table_name="sec_insider_transactions",
        schema="platform",
    )
    op.drop_index(
        "ix_sec_insider_transactions_ticker_date",
        table_name="sec_insider_transactions",
        schema="platform",
    )
    op.drop_table("sec_insider_transactions", schema="platform")
