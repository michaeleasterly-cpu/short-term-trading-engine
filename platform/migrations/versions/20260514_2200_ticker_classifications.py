"""ticker_classifications — stock vs ETF + ETF subtype metadata.

Until 2026-05-14 the platform had no way to distinguish ETFs from
stocks. Two concrete pains:

1. Dashboard's coverage-gaps check counted ETFs (AAXJ, ACWI, BNDX, …)
   as "missing fundamentals" and turned the row red even though FMP
   legitimately doesn't cover them. 922 of 1,274 T1+T2 tickers fell
   into this false-positive.

2. The planned sentinel engine will only trade INVERSE ETFs (SH, SDS,
   SDOW, SQQQ, …). It needs a deterministic filter for the inverse
   subset, not a brittle name regex.

Schema follows the expert recommendation (2026-05-14 design review):
one table, one row per ticker, asset_class enum with a CHECK
constraint that the etf_* columns are only populated when
``asset_class != 'stock'``.

Refresh cadence: ~monthly (asset class essentially never changes for a
given ticker; only new listings need adding). Populated by a separate
ingest handler that pulls Alpaca ``/v2/assets`` for the binary flag +
FMP ``/profile`` for ETF subtype enrichment.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260514_2200"
down_revision: str | None = "20260514_2000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ticker_classifications",
        sa.Column("ticker", sa.Text(), primary_key=True),
        sa.Column("asset_class", sa.Text(), nullable=False),
        sa.Column("etf_inverse", sa.Boolean(), nullable=True),
        sa.Column("etf_leverage", sa.Numeric(4, 2), nullable=True),
        sa.Column("etf_category", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "last_updated",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "asset_class IN ('stock', 'etf', 'fund')",
            name="ticker_classifications_asset_class_chk",
        ),
        # Physical-truth gate: stock rows must NOT carry ETF subtype fields.
        sa.CheckConstraint(
            "asset_class = 'stock' AND etf_inverse IS NULL "
            "AND etf_leverage IS NULL AND etf_category IS NULL "
            "OR asset_class IN ('etf', 'fund')",
            name="ticker_classifications_etf_fields_chk",
        ),
        schema="platform",
    )
    # Partial index on the (asset_class, etf_inverse) pair — only stores
    # ETF rows. Sentinel's universe filter
    # ``WHERE asset_class='etf' AND etf_inverse=true`` becomes <1ms.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_ticker_classifications_etf
        ON platform.ticker_classifications (asset_class, etf_inverse)
        WHERE asset_class = 'etf'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS platform.ix_ticker_classifications_etf")
    op.drop_table("ticker_classifications", schema="platform")
