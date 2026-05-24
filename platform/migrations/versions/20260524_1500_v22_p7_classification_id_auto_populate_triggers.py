"""v2.2 P7 — BEFORE INSERT triggers that auto-populate classification_id
from platform.ticker_history (ticker-at-row-date lookup).

The remaining v2.2 work: every new row in a ticker-bearing child table
should carry the correct `classification_id` based on the ticker's
identity AT THE ROW'S DATE — not just the live "current ticker" mapping.
Rationale: if a company renames (re-IPO, ticker rotation), historical
rows ingested AFTER the rename should still resolve to the historical
classification_id, not the new one.

Right now `ticker_history` has 13,672 rows but ZERO historical entries
(every row is `valid_to IS NULL`). So today the trigger logic always
returns the same answer as "join on current ticker" — pure architectural
future-proofing. The day a rename event lands, every producer
transparently does the right thing.

## Pattern: BEFORE INSERT trigger per table

Each Path-A child table gets a BEFORE INSERT trigger that:
  1. No-ops if `classification_id` is already populated (caller already
     resolved it — explicit always wins).
  2. Otherwise: looks up `ticker_history` for the (ticker, as_of_date)
     valid row and stamps `NEW.classification_id`.
  3. If no ticker_history row matches, leaves NULL — Path-A's nullable
     contract handles this (no FK violation).

The per-table date column varies — each table gets a specialized trigger
function that knows its own date column. The 14 tables + their as_of
expression:

  Table                       Ticker col   As-of column / expression
  --------------------------  -----------  -----------------------------
  prices_daily                ticker       date
  fundamentals_quarterly      ticker       period_end_date
  corporate_actions           ticker       action_date
  earnings_events             ticker       event_date
  short_interest              ticker       settlement_date
  insider_sentiment           symbol       make_date(year, month, 1)
  insider_transactions        ticker       filing_date
  liquidity_tiers             ticker       last_updated::date
  options_max_pain            symbol       observed_date
  sec_material_events         ticker       filing_date
  social_sentiment            ticker       date
  spread_observations         ticker       observed_at::date
  borrow_rates                ticker       date
  universe_candidates         ticker       as_of_date

## Performance + bulk-insert escape hatch

The trigger fires per row. For daily handlers (~7,600 rows/day for
daily_bars), the ~1 ms/row DB lookup adds ~7-8 seconds — negligible.

For TRULY high-volume backfills (e.g., 21M-row survivorship_backfill),
the caller should EITHER:
  (a) populate classification_id in the INSERT VALUES explicitly via the
      same ticker_history JOIN done server-side in the INSERT statement
      (preferred — single round-trip), OR
  (b) `SET LOCAL session_replication_role = 'replica'` to disable
      triggers for the duration of the bulk write, then run the
      `parent_resolver_orphan_backfill` stage afterwards.

## Idempotent + DROP IF EXISTS

The migration creates 14 functions + 14 triggers. Re-running drops first
(idempotency) so a partial-replay is safe.

Revision ID: 20260524_1500
Revises: 20260524_1400
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_1500"
down_revision: str | None = "20260524_1400"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


# (table, ticker_column, as_of_expression).
# as_of_expression is the SQL fragment that yields a DATE value for the
# ticker_history lookup, evaluated against NEW.
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
        trigger_name = f"tg_{table}_classification_id"

        # Idempotency: drop existing trigger + function first.
        op.execute(
            f"DROP TRIGGER IF EXISTS {trigger_name} ON platform.{table}"
        )
        op.execute(f"DROP FUNCTION IF EXISTS platform.{fn_name}()")

        # Per-table trigger function — knows its own ticker column +
        # as-of date expression. Looks up ticker_history's valid row at
        # the as-of date (valid_from <= as_of <= COALESCE(valid_to, infinity)).
        op.execute(
            f"""
            CREATE FUNCTION platform.{fn_name}()
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

        # BEFORE INSERT only. UPDATE is rare on these tables (append-only
        # ingestion pattern); if a row's ticker or date changes the caller
        # should explicitly re-resolve classification_id.
        op.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE INSERT ON platform.{table}
            FOR EACH ROW
            EXECUTE FUNCTION platform.{fn_name}();
            """
        )


def downgrade() -> None:
    for table, _ticker_col, _as_of_expr in _TARGETS:
        fn_name = f"tg_set_classification_id_{table}"
        trigger_name = f"tg_{table}_classification_id"
        op.execute(
            f"DROP TRIGGER IF EXISTS {trigger_name} ON platform.{table}"
        )
        op.execute(f"DROP FUNCTION IF EXISTS platform.{fn_name}()")
