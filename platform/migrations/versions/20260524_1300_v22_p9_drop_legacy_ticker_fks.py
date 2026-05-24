"""v2.2 P9 — drop the 14 legacy ticker-keyed FKs into ticker_classifications.

Per v2.2 spec — the v2.1 Phase 2 FKs (ADD CONSTRAINT NOT VALID, never
VALIDATEd) were a transitional structure that became redundant when v2.2
shipped the validated `classification_id` FKs in P6 (migration 20260524_0500,
20260524_0600, 20260524_0700/0701).

Each of the 14 ticker-bearing child tables now has TWO FKs into
ticker_classifications:
  - validated `<child>_classification_id_fk` on (classification_id) → id
  - NOT VALID `fk_<child>_ticker` on (ticker|symbol) → ticker  ← THIS migration drops

Postgres NOT VALID FKs are not enforced — they don't block writes that
violate them. They appear in `\\d` output + occupy a small amount of catalog
space, and they're a drift surface for future devs who might mistake them
for live constraints. Dropping is purely cleanup; no behaviour change.

The 14 FKs (one per Path-A / Path-B child table from v2.2 P6):

  1.  fk_borrow_rates_ticker          (borrow_rates.ticker)
  2.  fk_corporate_actions_ticker     (corporate_actions.ticker)
  3.  fk_earnings_events_ticker       (earnings_events.ticker)
  4.  fk_fundamentals_quarterly_ticker (fundamentals_quarterly.ticker)
  5.  fk_insider_sentiment_symbol     (insider_sentiment.symbol)
  6.  fk_insider_transactions_ticker  (insider_transactions.ticker)
  7.  fk_liquidity_tiers_ticker       (liquidity_tiers.ticker)
  8.  fk_options_max_pain_symbol      (options_max_pain.symbol)
  9.  fk_prices_daily_ticker          (prices_daily.ticker)
  10. fk_sec_material_events_ticker   (sec_material_events.ticker)
  11. fk_short_interest_ticker        (short_interest.ticker)
  12. fk_social_sentiment_ticker      (social_sentiment.ticker)
  13. fk_spread_observations_ticker   (spread_observations.ticker)
  14. fk_universe_candidates_ticker   (universe_candidates.ticker)

All 14 DROP CONSTRAINT IF EXISTS run in a single alembic transaction.
Each is metadata-only (no table rewrite, no index drop), so the migration
completes in milliseconds.

Revision ID: 20260524_1300
Revises: 20260524_1200
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_1300"
down_revision: str | None = "20260524_1200"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


_LEGACY_FK_BY_TABLE: tuple[tuple[str, str], ...] = (
    ("borrow_rates",            "fk_borrow_rates_ticker"),
    ("corporate_actions",       "fk_corporate_actions_ticker"),
    ("earnings_events",         "fk_earnings_events_ticker"),
    ("fundamentals_quarterly",  "fk_fundamentals_quarterly_ticker"),
    ("insider_sentiment",       "fk_insider_sentiment_symbol"),
    ("insider_transactions",    "fk_insider_transactions_ticker"),
    ("liquidity_tiers",         "fk_liquidity_tiers_ticker"),
    ("options_max_pain",        "fk_options_max_pain_symbol"),
    ("prices_daily",            "fk_prices_daily_ticker"),
    ("sec_material_events",     "fk_sec_material_events_ticker"),
    ("short_interest",          "fk_short_interest_ticker"),
    ("social_sentiment",        "fk_social_sentiment_ticker"),
    ("spread_observations",     "fk_spread_observations_ticker"),
    ("universe_candidates",     "fk_universe_candidates_ticker"),
)


def upgrade() -> None:
    for table_name, fk_name in _LEGACY_FK_BY_TABLE:
        op.execute(
            f"ALTER TABLE platform.{table_name} "
            f"DROP CONSTRAINT IF EXISTS {fk_name}"
        )


def downgrade() -> None:
    # Recreate as NOT VALID so the original v2.1 Phase 2 shape is restored
    # without paying the validation cost (the constraints were never enforced
    # in production anyway). The column-name asymmetry (insider_sentiment uses
    # `symbol`, options_max_pain uses `symbol`, rest use `ticker`) mirrors the
    # original schema.
    _column_overrides = {
        "insider_sentiment":   "symbol",
        "options_max_pain":    "symbol",
    }
    for table_name, fk_name in _LEGACY_FK_BY_TABLE:
        col = _column_overrides.get(table_name, "ticker")
        op.execute(
            f"ALTER TABLE platform.{table_name} "
            f"ADD CONSTRAINT {fk_name} "
            f"FOREIGN KEY ({col}) "
            f"REFERENCES platform.ticker_classifications(ticker) "
            f"NOT VALID"
        )
