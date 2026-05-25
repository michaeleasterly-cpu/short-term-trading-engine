"""P0_5 (operator-corrected 2026-05-25) — freeze ``source='alpaca'``
on ``platform.prices_daily`` and demote alpaca in
``platform._source_priority``.

Replaces the data-session's original "tradier freeze" framing. The
operator's standing rule (memory ``feedback_no_alpaca_for_daily_
prices_backfill``, 2026-05-25) sets:

    1. **FMP** — primary
    2. **Tradier** — secondary / acceptable (operator 2026-05-25:
       "tradier still works, use it for backfill while it lasts but
       don't count on it for future")
    3. **Alpaca** — **NEVER** for daily-bar backfill into
       prices_daily. Alpaca's close-date semantics differ from
       FMP/Tradier (session-boundary / timezone aggregation
       differs); per-row inconsistency contaminates backtest +
       engine signals. Alpaca SIP also "doesn't work" anymore
       (operator 2026-05-25); the iex|sip flag fallback paths are
       effectively dead.

The 2,767,965 existing ``source='alpaca'`` rows are tolerated as
historical artifact and being backfilled to tradier/fmp by the
data session (cross-agent handoff
``/cross-agent/engine-to-data/2026-05-25-alpaca-backfill-scope.md``).
This migration only blocks NEW alpaca writes via ``NOT VALID``.

Two artifacts in one migration

A. ``prices_daily_no_new_alpaca`` CHECK constraint:
   ALTER TABLE platform.prices_daily ADD CONSTRAINT
   prices_daily_no_new_alpaca CHECK (source <> 'alpaca') NOT VALID.
   ``NOT VALID`` skips the one-time pass of the existing 2.7M
   alpaca rows; future INSERTs and UPDATEs that would set
   source='alpaca' are rejected. DELETE+INSERT (the canonical
   replacement path) is unaffected.

B. ``platform._source_priority(s)`` REPLACEd to demote alpaca:

       fmp     4   primary
       tradier 3   acceptable secondary (was 1 — promoted)
       sip     2   Alpaca SIP — flag fallback (operator notes
                   "doesn't work" but kept for completeness)
       iex     1   Alpaca IEX free-tier — flag fallback
       alpaca  0   frozen by THIS migration's CHECK; existing
                   rows are historical (was 3 — demoted)
       *       0   unknown / NULL (lowest)

   The P4 provenance-downgrade guard on ``_upsert_bars`` /
   ``stage_then_promote_bars`` now correctly lets fmp + tradier
   overwrite existing alpaca rows (priority(fmp/tradier) >=
   priority(alpaca)=0). This is the data session's backfill path.

Revision ID: 20260525_1200
Revises: 20260525_0900
Create Date: 2026-05-25
"""
from alembic import op

revision: str = "20260525_1200"
# Chains after 20260525_0900 (P3 staging). The earlier
# 20260525_1100_freeze_tradier_source.py was applied to live DB
# then immediately rolled back when the operator pointed out the
# direction was inverted (per memory feedback_no_alpaca_for_
# daily_prices_backfill); that migration's file was never merged
# to main, so the chain on main is 20260525_0900 → THIS.
down_revision: str | None = "20260525_0900"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # Part A — freeze NEW source='alpaca' writes (existing rows
    # untouched via NOT VALID).
    op.execute(
        """
        ALTER TABLE platform.prices_daily
        ADD CONSTRAINT prices_daily_no_new_alpaca
        CHECK (source <> 'alpaca') NOT VALID
        """
    )

    # Part B — REPLACE the priority function with the corrected
    # ordering: tradier promoted to 3 (acceptable secondary),
    # alpaca demoted to 0 (frozen by CHECK; existing rows
    # historical-only).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION platform._source_priority(s text)
        RETURNS smallint AS $$
            SELECT CASE s
                WHEN 'fmp'     THEN 4::smallint
                WHEN 'tradier' THEN 3::smallint
                WHEN 'sip'     THEN 2::smallint
                WHEN 'iex'     THEN 1::smallint
                WHEN 'alpaca'  THEN 0::smallint
                ELSE 0::smallint
            END;
        $$ LANGUAGE SQL IMMUTABLE;
        """
    )


def downgrade() -> None:
    # Restore the original P4 priority function.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION platform._source_priority(s text)
        RETURNS smallint AS $$
            SELECT CASE s
                WHEN 'fmp'     THEN 4::smallint
                WHEN 'sip'     THEN 3::smallint
                WHEN 'alpaca'  THEN 3::smallint
                WHEN 'iex'     THEN 2::smallint
                WHEN 'tradier' THEN 1::smallint
                ELSE 0::smallint
            END;
        $$ LANGUAGE SQL IMMUTABLE;
        """
    )
    op.execute(
        """
        ALTER TABLE platform.prices_daily
        DROP CONSTRAINT IF EXISTS prices_daily_no_new_alpaca
        """
    )
