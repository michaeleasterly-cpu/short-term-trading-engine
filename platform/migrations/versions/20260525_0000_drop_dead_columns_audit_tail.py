"""Drop genuinely-dead columns flagged by 2026-05-24 audit + populate
issuers.country_of_incorp from ticker_classifications.country.

Audit found 18 ALL_NULL columns. This migration handles the
genuinely-dead ones with no plan to populate:

  Drop:
    sec_material_events.summary (text, 237,767 rows) — LLM-triage
      was the intended writer; removed 2026-05-22. Handler INSERTs
      explicit None. No reader anywhere.
    ingestion_metrics.{min_date, max_date, coverage_pct} — D2 metrics
      design that never got wired beyond `source, recorded_at,
      latest_date, status`. The latest_date column already serves
      the freshness check; the other three were aspirational.
    issuer_securities.{share_class, notes} — Path-A doesn't track
      share class or per-mapping notes; the 25 existing rows have
      no use for them.

  Populate:
    issuers.country_of_incorp — derive from ticker_classifications.country
      (already populated for all 5,202 US + 1,030 foreign issuers).
      The earlier ALL-NULL state was because the EDGAR-formerNames
      backfill stage didn't set country_of_incorp; we have the
      data elsewhere.

Kept as-is (intentional, by design):
  daemon_heartbeats.extra (JSONB extension)
  corporate_events.extra_terms (JSONB extra)
  risk_state.kill_switch_reason (set on kill switch fire)
  series_catalog.publish_day_of_month (set for monthly series)
  universe_candidates.reason (JSONB extension)
  allocations.{realized_vol, freeze_reason} (allocator design TODO)
  issuers.lei (would need OpenLEI lookup — deferred)
  ticker_classifications.{etf_category, gics_sector} (vendor-source
    TODO — deferred)

Revision ID: 20260525_0000
Revises: 20260524_1903
Create Date: 2026-05-25
"""
from alembic import op

revision: str = "20260525_0000"
down_revision: str | None = "20260524_1903"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # 1. Drop dead columns.
    op.execute("ALTER TABLE platform.sec_material_events DROP COLUMN IF EXISTS summary")
    op.execute(
        """
        ALTER TABLE platform.ingestion_metrics
            DROP COLUMN IF EXISTS min_date,
            DROP COLUMN IF EXISTS max_date,
            DROP COLUMN IF EXISTS coverage_pct
        """
    )
    op.execute(
        """
        ALTER TABLE platform.issuer_securities
            DROP COLUMN IF EXISTS share_class,
            DROP COLUMN IF EXISTS notes
        """
    )

    # 2. Populate issuers.country_of_incorp from ticker_classifications.
    # For each issuer, pick the (single) country of any of its
    # classifications. Multi-listing-country issuers are rare; first
    # match wins.
    op.execute(
        """
        UPDATE platform.issuers i
        SET country_of_incorp = sub.country
        FROM (
            SELECT DISTINCT ON (tc.cik) tc.cik, tc.country
            FROM platform.ticker_classifications tc
            WHERE tc.cik IS NOT NULL AND tc.country IS NOT NULL
            ORDER BY tc.cik, tc.id
        ) sub
        WHERE i.cik = sub.cik
          AND (i.country_of_incorp IS NULL OR i.country_of_incorp <> sub.country)
        """
    )


def downgrade() -> None:
    # Restore dropped columns (empty — original data is unrecoverable).
    op.execute("ALTER TABLE platform.sec_material_events ADD COLUMN IF NOT EXISTS summary TEXT")
    op.execute(
        """
        ALTER TABLE platform.ingestion_metrics
            ADD COLUMN IF NOT EXISTS min_date DATE,
            ADD COLUMN IF NOT EXISTS max_date DATE,
            ADD COLUMN IF NOT EXISTS coverage_pct NUMERIC
        """
    )
    op.execute(
        """
        ALTER TABLE platform.issuer_securities
            ADD COLUMN IF NOT EXISTS share_class TEXT,
            ADD COLUMN IF NOT EXISTS notes TEXT
        """
    )
    op.execute("UPDATE platform.issuers SET country_of_incorp = NULL")
