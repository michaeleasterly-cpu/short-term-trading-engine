"""Task #18 P1 — create platform.macro_data (bitemporal tall) + 3 shim VIEWs.

Per spec `docs/superpowers/specs/2026-05-23-task-18-macro-data-consolidation.md`.
Step 1 of the Fowler expand-contract migration:

  P1 (this) — CREATE TABLE + 3 shim VIEWs (_v suffix; parallel to live tables)
  P2 — backfill stage (chunked INSERT-SELECT from each of 3 source tables;
       hy_spread verbatim copy per project_hy_spread_sacred)
  P3 — producer double-write
  P4 — parity verification test
  P5 — cutover (rename live -> _legacy; rename _v views to original names)
  P6 — consumer migration
  P7 — drop _legacy tables

Schema (canonical bitemporal tall per FRED/ALFRED + Kimball SCD-2 +
typed XOR channels to avoid EAV anti-patterns):

  source         - provider ('fred','aaii','cnn_fear_greed',...)
  series_id      - within-provider series identifier
  observed_date  - valid-time (when the fact was true in the world)
  value_num      - numeric channel (NULL if categorical)
  value_text     - categorical channel (NULL if numeric)
  realtime_start - transaction-time start (defaults now(); backfill uses
                   source.recorded_at for verbatim preservation)
  realtime_end   - transaction-time end ('infinity' for current row)
  recorded_at    - clock-time we observed this row

Indexes:
  ix_macro_data_latest - partial on (series_id, observed_date DESC)
                         WHERE realtime_end='infinity'; the hot read path
  ix_macro_data_pit    - GIST on (series_id, tstzrange(realtime_start,
                         realtime_end)) for as-of-T @> containment queries
  ix_macro_data_source - partial on (source, observed_date DESC)
                         WHERE realtime_end='infinity'; show-all-FRED dashboards

Shim views (_v suffix; rename at P5 cutover):
  macro_indicators_v - 1:1 select from macro_data WHERE source='fred'
  aaii_sentiment_v   - pivot tall->wide via FILTER aggregation per date
  fear_greed_v       - same pivot pattern with the 9-column shape

btree_gist extension (installed in 'extensions' schema per 20260524_0200) is
required for GIST equality on the leading text column - verified live.

This migration does NOT move data. P2 backfill is a separate migration / stage.

Revision ID: 20260524_0900
Revises: 20260524_0800
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_0900"
down_revision: str | None = "20260524_0800"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.macro_data (
            source         text        NOT NULL,
            series_id      text        NOT NULL,
            observed_date  date        NOT NULL,
            value_num      numeric              NULL,
            value_text     text                 NULL,
            realtime_start timestamptz NOT NULL DEFAULT now(),
            realtime_end   timestamptz NOT NULL DEFAULT 'infinity',
            recorded_at    timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT macro_data_value_xor CHECK (
                (value_num IS NOT NULL)::int + (value_text IS NOT NULL)::int = 1
            ),
            CONSTRAINT macro_data_pit_pk PRIMARY KEY
                (source, series_id, observed_date, realtime_start)
        )
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_macro_data_latest
            ON platform.macro_data (series_id, observed_date DESC)
            WHERE realtime_end = 'infinity'
        """
    )

    # Expression GIST index for point-in-time @> containment. Extra parens
    # around the tstzrange expression are required by Postgres index syntax.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_macro_data_pit
            ON platform.macro_data USING GIST
                (series_id, (tstzrange(realtime_start, realtime_end)))
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_macro_data_source
            ON platform.macro_data (source, observed_date DESC)
            WHERE realtime_end = 'infinity'
        """
    )

    op.execute(
        """
        CREATE OR REPLACE VIEW platform.macro_indicators_v AS
        SELECT
            series_id     AS indicator,
            observed_date AS date,
            value_num     AS value,
            recorded_at
        FROM platform.macro_data
        WHERE source = 'fred' AND realtime_end = 'infinity'
        """
    )

    op.execute(
        """
        CREATE OR REPLACE VIEW platform.aaii_sentiment_v AS
        SELECT
            observed_date AS date,
            MAX(value_num) FILTER (WHERE series_id = 'bullish_pct') AS bullish_pct,
            MAX(value_num) FILTER (WHERE series_id = 'bearish_pct') AS bearish_pct,
            MAX(value_num) FILTER (WHERE series_id = 'neutral_pct') AS neutral_pct,
            MAX(recorded_at) AS recorded_at
        FROM platform.macro_data
        WHERE source = 'aaii' AND realtime_end = 'infinity'
        GROUP BY observed_date
        """
    )

    op.execute(
        """
        CREATE OR REPLACE VIEW platform.fear_greed_v AS
        SELECT
            observed_date AS date,
            MAX(value_num)  FILTER (WHERE series_id = 'score')                AS score,
            MAX(value_text) FILTER (WHERE series_id = 'label')                AS label,
            MAX(value_text) FILTER (WHERE series_id = 'direction')            AS direction,
            MAX(value_num)  FILTER (WHERE series_id = 'score_5d_ago')         AS score_5d_ago,
            MAX(value_num)  FILTER (WHERE series_id = 'volatility_component') AS volatility_component,
            MAX(value_num)  FILTER (WHERE series_id = 'credit_component')     AS credit_component,
            MAX(value_num)  FILTER (WHERE series_id = 'momentum_component')   AS momentum_component,
            MAX(value_num)  FILTER (WHERE series_id = 'safe_haven_component') AS safe_haven_component,
            MAX(recorded_at) AS recorded_at
        FROM platform.macro_data
        WHERE source = 'cnn_fear_greed' AND realtime_end = 'infinity'
        GROUP BY observed_date
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS platform.fear_greed_v")
    op.execute("DROP VIEW IF EXISTS platform.aaii_sentiment_v")
    op.execute("DROP VIEW IF EXISTS platform.macro_indicators_v")
    op.execute("DROP INDEX IF EXISTS platform.ix_macro_data_source")
    op.execute("DROP INDEX IF EXISTS platform.ix_macro_data_pit")
    op.execute("DROP INDEX IF EXISTS platform.ix_macro_data_latest")
    op.execute("DROP TABLE IF EXISTS platform.macro_data")
