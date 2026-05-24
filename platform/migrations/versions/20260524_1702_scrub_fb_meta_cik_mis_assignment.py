"""v2.2 ticker-reuse — scrub the bad Meta CIK assignment on the etf-FB row.

The Phase A orphan resolver (20260524_1500 era) looked up FB in the
EDGAR truth-set, found Meta's CIK 0001326801, and stamped it on the
ticker_classifications row for FB. But the FB row is actually for a
delisted ETF (`asset_class='etf'`, `source='alpaca_name'`, all 228
prices_daily bars 2025-06-26 → 2026-05-21 with `delisted=true`).

Meta (the social-media company) was renamed FROM Facebook in 2022,
and Facebook's old FB ticker was retired well before our 2025 data
window. The Meta CIK on this etf-FB row is purely a name-match defect
in the orphan resolver — the etf has nothing to do with Meta.

Side effect of the bad CIK: when the corporate_events_seed stage ran
the FB→META rename row, it created an `issuer_securities` mapping
linking Meta's issuer to the etf-FB classification (because the seed
stage looks up classification_id by ticker). That mapping is also
wrong and is removed here.

This migration:
  1. Removes `cik='0001326801'` from the etf-FB row in ticker_classifications.
  2. Deletes the `(CIK0001326801, etf-FB classification)` row from issuer_securities.

The Meta issuer record itself stays — it correctly represents Meta
Platforms. Its issuer_securities mapping for the actual META ticker
(USES54XOAUJBA0 or similar) remains untouched.

The downgrade is intentionally a no-op: re-adding the wrong CIK
would re-introduce the defect.

Revision ID: 20260524_1702
Revises: 20260524_1701
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_1702"
down_revision: str | None = "20260524_1701"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # 1. Find the etf-FB classification_id (defensive — could be USEZ26OTVHAA88
    #    or whatever id was assigned; use ticker+asset_class as the natural key).
    # 2. Delete the bad issuer_securities mapping for that classification.
    # 3. NULL the cik on the etf-FB row.
    op.execute(
        """
        DELETE FROM platform.issuer_securities
        WHERE issuer_id = 'CIK0001326801'
          AND classification_id IN (
              SELECT id FROM platform.ticker_classifications
              WHERE ticker = 'FB' AND asset_class = 'etf' AND cik = '0001326801'
          )
        """
    )
    op.execute(
        """
        UPDATE platform.ticker_classifications
        SET cik = NULL
        WHERE ticker = 'FB' AND asset_class = 'etf' AND cik = '0001326801'
        """
    )


def downgrade() -> None:
    # Intentional no-op: re-adding the wrong CIK would re-introduce
    # the defect. If a real Facebook stock row is added in the future
    # with the correct asset_class, it will independently carry the
    # CIK on its own row.
    pass
