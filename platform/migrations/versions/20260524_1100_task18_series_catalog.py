"""Task #18 follow-on — platform.series_catalog: per-(source, series_id) metadata.

Per operator directive 2026-05-24 (originally spec §12 out-of-scope):
brings the per-series metadata that lived as Python constants
(INDICATOR_CADENCE, INDICATOR_SERIES, INDICATOR_WEEKLY_ANCHOR_WEEKDAY)
into the database. Makes platform.macro_data self-describing so consumers
(engines, dashboard, lab, publishing/stelib) can resolve a series's
cadence / unit / vendor_series_id without importing Python constants.

Schema:
  PRIMARY KEY (source, series_id) — pairs with platform.macro_data's
  (source, series_id, observed_date, realtime_start) natural key. No FK
  to macro_data (macro_data's PK is bitemporal; can't FK to that grain).
  Series rows may exist in the catalog before any observations land
  (catalog-first), and macro_data rows must always have a catalog entry
  (enforced by a separate validation check).

Columns:
  source                  - 'fred' | 'aaii' | 'cnn_fear_greed' | ...
  series_id               - canonical id used in macro_data
  vendor_series_id        - vendor's id (e.g. FRED 'NFCI' for our 'nfci')
  description             - human-readable label
  unit                    - 'percent' | 'index_value' | 'count' | ...
  frequency               - 'daily' | 'weekly' | 'monthly' | 'derived'
  publish_weekday         - ISO 1-7 for weekly series (1=Mon, 4=Thu)
  publish_day_of_month    - day-of-month for monthly with stable schedule
  publish_lag_days        - days from observed_date to vendor publication
                            (e.g. SOFR T+1 = 1)
  is_seasonally_adjusted  - SA vs NSA for FRED economic series
  is_derived              - true if computed by us (not pulled externally)
  sacred                  - true for operator-curated history (hy_spread)
                            that must NEVER be re-derived or overwritten
  publication_calendar_url - vendor's release calendar page for ops audit
  notes                   - free-text for caveats (e.g. EPU 30d-rolling revisions)
  created_at / updated_at - bookkeeping

Indexes:
  ix_series_catalog_source - source-bucket dashboards
  ix_series_catalog_freq   - cadence-class queries (e.g. all daily series)

The frequency CHECK and publish_weekday CHECK enforce the enum-equivalent
values from the existing INDICATOR_CADENCE constants so drift between
the catalog and the Python constants is hard-coded out at the DB layer.

Revision ID: 20260524_1100
Revises: 20260524_1000
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_1100"
down_revision: str | None = "20260524_1000"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.series_catalog (
            source                    text        NOT NULL,
            series_id                 text        NOT NULL,
            vendor_series_id          text                NULL,
            description               text                NULL,
            unit                      text                NULL,
            frequency                 text        NOT NULL,
            publish_weekday           smallint            NULL,
            publish_day_of_month      smallint            NULL,
            publish_lag_days          smallint            NULL,
            is_seasonally_adjusted    boolean             NULL,
            is_derived                boolean     NOT NULL DEFAULT false,
            sacred                    boolean     NOT NULL DEFAULT false,
            publication_calendar_url  text                NULL,
            notes                     text                NULL,
            created_at                timestamptz NOT NULL DEFAULT now(),
            updated_at                timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT series_catalog_pk PRIMARY KEY (source, series_id),
            CONSTRAINT series_catalog_freq_chk CHECK (
                frequency IN ('daily','weekly','monthly','quarterly','derived')
            ),
            CONSTRAINT series_catalog_weekday_chk CHECK (
                publish_weekday IS NULL OR publish_weekday BETWEEN 1 AND 7
            ),
            CONSTRAINT series_catalog_dom_chk CHECK (
                publish_day_of_month IS NULL OR publish_day_of_month BETWEEN 1 AND 31
            )
        )
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_series_catalog_source
            ON platform.series_catalog (source)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_series_catalog_freq
            ON platform.series_catalog (frequency)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS platform.ix_series_catalog_freq")
    op.execute("DROP INDEX IF EXISTS platform.ix_series_catalog_source")
    op.execute("DROP TABLE IF EXISTS platform.series_catalog")
