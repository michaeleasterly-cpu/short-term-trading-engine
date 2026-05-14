"""ticker_classifications — allow 'spac' as an asset_class.

The dashboard's coverage-gap classifier (post-2026-05-14 ETF
correction) still showed 187/514 stocks "missing fundamentals." On
inspection ~184 of those are SPACs (blank-check companies + their
warrants + units): AACO, AACOU, AEAQ, ARCIU, etc. — these legitimately
have no ``fundamentals_quarterly`` rows because they're shells with no
operating business until a merger.

Adding ``'spac'`` to the asset_class CHECK so the classifier can
distinguish stock-with-fundamentals vs SPAC-with-no-fundamentals.
Dashboard's coverage denominator is ``asset_class = 'stock'`` so SPACs
get excluded for free once classified.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260514_2300"
down_revision: str | None = "20260514_2200"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT IF EXISTS ticker_classifications_asset_class_chk"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD CONSTRAINT ticker_classifications_asset_class_chk "
        "CHECK (asset_class IN ('stock', 'etf', 'fund', 'spac'))"
    )
    # The etf_fields invariant is the same: only ETFs/funds carry the
    # etf_* fields. SPACs have NULL on all of them (like stocks).
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT IF EXISTS ticker_classifications_etf_fields_chk"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD CONSTRAINT ticker_classifications_etf_fields_chk CHECK ("
        "  asset_class IN ('stock', 'spac') "
        "  AND etf_inverse IS NULL "
        "  AND etf_leverage IS NULL "
        "  AND etf_category IS NULL "
        "  OR asset_class IN ('etf', 'fund')"
        ")"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT IF EXISTS ticker_classifications_asset_class_chk"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD CONSTRAINT ticker_classifications_asset_class_chk "
        "CHECK (asset_class IN ('stock', 'etf', 'fund'))"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT IF EXISTS ticker_classifications_etf_fields_chk"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD CONSTRAINT ticker_classifications_etf_fields_chk CHECK ("
        "  asset_class = 'stock' AND etf_inverse IS NULL "
        "  AND etf_leverage IS NULL AND etf_category IS NULL "
        "  OR asset_class IN ('etf', 'fund')"
        ")"
    )
