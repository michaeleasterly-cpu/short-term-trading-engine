"""Engine-abstraction universe view — `platform.v_universe`.

Adds a read-only VIEW that joins the three tables engine universe
enumeration cares about — `ticker_classifications` (identity + attrs),
`ticker_history` (SCD-2 per-security ticker timeline), and
`liquidity_tiers` (T1/T2 filtering) — and exposes them keyed on the
TKR-14 surrogate `classification_id`.

Used by `tpcore.data.repositories.universe.UniverseRepo` to replace
the scattered ad-hoc `SELECT ticker FROM platform.liquidity_tiers ...`
and `SELECT DISTINCT ticker FROM platform.fundamentals_quarterly ...`
patterns currently embedded in 7 engines. Callers add WHERE clauses
on `valid_from`/`valid_to` (for as-of-date), `liquidity_tier`,
`asset_class`, `country` as needed.

Shape (one row per ticker_history row — same classification_id can
appear multiple times when its ticker has changed; the repo filters
to one row per classification_id via as-of-date):

  classification_id  text  (FK → ticker_classifications.id)
  ticker_at_date     text  (the ticker_history.ticker value)
  current_ticker     text  (the latest ticker_classifications.current_ticker)
  asset_class        text  (stock / etf / spac / fund / ...)
  country            char(2)  (ISO-3166-1 alpha-2; nullable for the 12% of
                               unresolved rows in ticker_classifications)
  status             text  (active / active_when_issued / delisted / ...)
  liquidity_tier     int   (NULL if no liquidity_tiers row; otherwise 1-N)
  valid_from         date  (the ticker_history validity start)
  valid_to           date  (NULL if currently-active; date otherwise)

Reversible: downgrade drops the view.

Revision ID: 20260524_2000
Revises: 20260524_1903
Create Date: 2026-05-24
"""

from alembic import op

revision: str = "20260524_2000"
down_revision: str | None = "20260524_1903"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE VIEW platform.v_universe AS
        SELECT
            tc.id              AS classification_id,
            th.ticker          AS ticker_at_date,
            tc.current_ticker  AS current_ticker,
            tc.asset_class     AS asset_class,
            tc.country         AS country,
            tc.status          AS status,
            lt.tier            AS liquidity_tier,
            th.valid_from      AS valid_from,
            th.valid_to        AS valid_to
        FROM platform.ticker_classifications tc
        JOIN platform.ticker_history th
          ON th.classification_id = tc.id
        LEFT JOIN platform.liquidity_tiers lt
          ON lt.classification_id = tc.id
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS platform.v_universe")
