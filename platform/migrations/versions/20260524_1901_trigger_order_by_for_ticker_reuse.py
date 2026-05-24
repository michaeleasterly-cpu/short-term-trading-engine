"""Fix ticker-reuse race in the 14 P7 trigger functions.

The trigger functions added in `20260524_1500_v22_p7_classification_id_auto_populate_triggers`
do:

    SELECT classification_id INTO NEW.classification_id
    FROM platform.ticker_history
    WHERE ticker = NEW.<ticker_col>
      AND valid_from <= <as_of_expr>
      AND (valid_to IS NULL OR valid_to >= <as_of_expr>)
    LIMIT 1;

Without `ORDER BY`, the row PostgreSQL returns is non-deterministic.
For the steady-state today (every ticker has exactly one
`ticker_history` row, no reuse yet) this is harmless. But the
ticker-reuse architecture added in `20260524_1700` is designed to
support multiple `ticker_history` rows per ticker in disjoint
windows. The moment a real reuse happens, the trigger could resolve
to either classification_id.

Engine-abstraction session (2026-05-24 handoff) flagged this as
defect #1. Fix: add `ORDER BY valid_from DESC` so the most-recent
matching window wins. This is unambiguous because the GIST exclude
constraint on `ticker_history` (added 2026-05-23) prevents range
overlap, so the highest-`valid_from` row whose window contains the
as-of date is uniquely the right one.

Rewrites all 14 trigger functions (table list + as-of expressions
copied verbatim from the original migration); the trigger objects
themselves don't change.

Revision ID: 20260524_1901
Revises: 20260524_1900
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_1901"
down_revision: str | None = "20260524_1900"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


# (table, ticker_column, as_of_expression) — identical to 20260524_1500.
_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("prices_daily",           "ticker", "NEW.date"),
    ("fundamentals_quarterly", "ticker", "NEW.period_end_date"),
    ("corporate_actions",      "ticker", "NEW.action_date"),
    ("earnings_events",        "ticker", "NEW.event_date"),
    ("short_interest",         "ticker", "NEW.settlement_date"),
    ("insider_sentiment",      "symbol", "make_date(NEW.year, NEW.month, 1)"),
    ("insider_transactions",   "ticker", "NEW.filing_date"),
    ("liquidity_tiers",        "ticker", "NEW.last_updated::date"),
    ("options_max_pain",       "symbol", "NEW.observed_date"),
    ("sec_material_events",    "ticker", "NEW.filing_date"),
    ("social_sentiment",       "ticker", "NEW.date"),
    ("spread_observations",    "ticker", "NEW.observed_at::date"),
    ("borrow_rates",           "ticker", "NEW.date"),
    ("universe_candidates",    "ticker", "NEW.as_of_date"),
)


def upgrade() -> None:
    for table, ticker_col, as_of_expr in _TARGETS:
        fn_name = f"tg_set_classification_id_{table}"
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION platform.{fn_name}()
            RETURNS TRIGGER AS $$
            BEGIN
                IF NEW.classification_id IS NULL THEN
                    SELECT classification_id INTO NEW.classification_id
                    FROM platform.ticker_history
                    WHERE ticker = NEW.{ticker_col}
                      AND valid_from <= {as_of_expr}
                      AND (valid_to IS NULL OR valid_to >= {as_of_expr})
                    ORDER BY valid_from DESC
                    LIMIT 1;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )


def downgrade() -> None:
    # Restore the original (race-prone) form.
    for table, ticker_col, as_of_expr in _TARGETS:
        fn_name = f"tg_set_classification_id_{table}"
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION platform.{fn_name}()
            RETURNS TRIGGER AS $$
            BEGIN
                IF NEW.classification_id IS NULL THEN
                    SELECT classification_id INTO NEW.classification_id
                    FROM platform.ticker_history
                    WHERE ticker = NEW.{ticker_col}
                      AND valid_from <= {as_of_expr}
                      AND (valid_to IS NULL OR valid_to >= {as_of_expr})
                    LIMIT 1;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
