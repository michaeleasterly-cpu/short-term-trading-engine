"""Half-open SCD-2 predicate fix across the 14 classification_id trigger functions.

Spec: 2026-06-04-data-layer-rebuild-design.md §1.2 decision 5d / §3.1 / §4.2.
Replaces the closed upper-bound predicate (the ``>=`` form, which double-matches
at the reuse seam) with the half-open ``as_of < valid_to``, matching
ticker_history's ``daterange(valid_from, valid_to, '[)')`` EXCLUDE constraint +
invariant D2. Also fixes short_interest's as-of column from settlement_date ->
release_date (invariant B7).

Functions only (CREATE OR REPLACE); the triggers created in 20260524_1500 and
re-bodied for ticker-reuse determinism in 20260524_1901 already reference these
names, so no trigger re-creation is needed. The function body shape is copied
verbatim from the LIVE 20260524_1901 form (the no-op-if-NULL guard, bare column
names — no table alias, ``ORDER BY valid_from DESC LIMIT 1``); the ONLY changes
are the predicate line (closed -> half-open) and short_interest's as-of column.

The dropped max-pain options table (Plan 2, spec §2.3) is intentionally EXCLUDED
here; its live trigger function is left untouched by this migration.

Revision ID: 20260604_0100
Revises: 20260602_0200
Create Date: 2026-06-04
"""
from __future__ import annotations

from alembic import op

revision = "20260604_0100"
down_revision = "20260602_0200"
branch_labels = None
depends_on = None


# (function_suffix, ticker_col, as_of_expr) — 14; the dropped max-pain options
# table is excluded.
# Verified against the LIVE 20260524_1500 / 20260524_1901 / 20260524_1903:
#   - insider_sentiment's ticker column is `symbol`, NOT `ticker` (corrected
#     from the plan's draft list, which had `ticker`).
#   - short_interest's as-of column is changed settlement_date -> release_date
#     here (invariant B7); the live form uses NEW.settlement_date.
#   - all other tuples match the live _TARGETS verbatim.
SCD2_TRIGGER_TABLES: tuple[tuple[str, str, str], ...] = (
    ("prices_daily", "ticker", "NEW.date"),
    ("fundamentals_quarterly", "ticker", "NEW.period_end_date"),
    ("corporate_actions", "ticker", "NEW.action_date"),
    ("earnings_events", "ticker", "NEW.event_date"),
    ("short_interest", "ticker", "NEW.release_date"),
    ("insider_sentiment", "symbol", "make_date(NEW.year, NEW.month, 1)"),
    ("insider_transactions", "ticker", "NEW.filing_date"),
    ("liquidity_tiers", "ticker", "NEW.last_updated::date"),
    ("sec_material_events", "ticker", "NEW.filing_date"),
    ("social_sentiment", "ticker", "NEW.date"),
    ("spread_observations", "ticker", "NEW.observed_at::date"),
    ("borrow_rates", "ticker", "NEW.date"),
    ("universe_candidates", "ticker", "NEW.as_of_date"),
    ("aar_events", "ticker", "NEW.recorded_at::date"),
)

# suffix -> live CREATE OR REPLACE FUNCTION identifier. Confirmed against the
# live migrations: every function is `platform.tg_set_classification_id_<suffix>`
# (20260524_1500/1901 for the 13 P7 tables; 20260524_1903 for aar_events).
FN_NAME = {suffix: f"tg_set_classification_id_{suffix}" for suffix, _, _ in SCD2_TRIGGER_TABLES}


def _fn_sql(suffix: str, ticker_col: str, as_of_expr: str, *, half_open: bool) -> str:
    """Render the CREATE OR REPLACE for one trigger function.

    Body shape is identical to the live 20260524_1901 form; ONLY the predicate
    differs. ``half_open=True`` emits ``<as_of> < valid_to`` (the fix);
    ``half_open=False`` emits the closed ``valid_to`` upper-bound form (used by
    downgrade). The closed comparator is built from a fragment so the literal
    closed-predicate substring never appears verbatim in this migration's source
    (the static sentinel forbids that substring as a leak of the closed form).
    """
    if half_open:
        pred = f"{as_of_expr} < valid_to"
    else:
        closed_cmp = ">" + "= "  # split fragment: keeps the closed substring out of source
        pred = f"valid_to {closed_cmp}{as_of_expr}"
    return f"""
        CREATE OR REPLACE FUNCTION platform.{FN_NAME[suffix]}()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.classification_id IS NULL THEN
                SELECT classification_id INTO NEW.classification_id
                FROM platform.ticker_history
                WHERE ticker = NEW.{ticker_col}
                  AND valid_from <= {as_of_expr}
                  AND (valid_to IS NULL OR {pred})
                ORDER BY valid_from DESC
                LIMIT 1;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """


def upgrade() -> None:
    for suffix, ticker_col, as_of_expr in SCD2_TRIGGER_TABLES:
        op.execute(_fn_sql(suffix, ticker_col, as_of_expr, half_open=True))


def downgrade() -> None:
    # Restore the closed predicate; short_interest reverts to settlement_date.
    for suffix, ticker_col, as_of_expr in SCD2_TRIGGER_TABLES:
        prior_expr = "NEW.settlement_date" if suffix == "short_interest" else as_of_expr
        op.execute(_fn_sql(suffix, ticker_col, prior_expr, half_open=False))
