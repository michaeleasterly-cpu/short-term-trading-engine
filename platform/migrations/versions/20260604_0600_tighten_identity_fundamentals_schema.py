"""Plan 2 — tighten identity + fundamentals schema on the empty tables (spec §3.1/§3.2).

Runs AFTER the Task-7 wipe (the tables are empty, so these are clean/fast):
  - ticker_classifications.lifetime_start: DROP the '1900-01-01' DEFAULT (stays
    NOT NULL, no default) so a load that fails to populate FPFD errors instead of
    silently sentineling (spec §3.1 / invariant A6).
  - fundamentals_quarterly: replace the surrogate PK + UNIQUE(ticker, filing_date)
    with the 3-part natural PK (ticker, period_end_date, filing_date) —
    restatement-preserving (spec §1.2 decision 8 / §3.2).
  - corporate_events.event_kind: NO CHANGE — the live CHECK (verified 2026-06-04)
    already admits 'delisting', 'bankruptcy_reorg', 'bankruptcy_liquidation', so
    the re-ingest can absorb the dropped ticker_lifecycle_events without a CHECK
    extension. Documented here so the no-op is intentional, not an omission.

Live introspection (2026-06-04, alembic head pre-apply 20260604_0500):
  * fundamentals_quarterly PK    = ``fundamentals_quarterly_pkey`` on (id)
  * fundamentals_quarterly UNIQUE = ``uq_fundamentals_ticker_filing`` on (ticker, filing_date)
  * period_end_date + filing_date are ALREADY NOT NULL (the SET NOT NULL below are
    safe idempotent no-ops, kept per the plan for defense on a re-run/empty table).
  * NOTHING FKs fundamentals_quarterly.id (pg_constraint confrelid scan → empty),
    so swapping the PK off ``id`` is safe. ``id`` is retained as a plain column.
"""
from __future__ import annotations

from alembic import op

revision = "20260604_0600"
down_revision = "20260604_0500"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ALTER COLUMN lifetime_start DROP DEFAULT"
    )

    # fundamentals_quarterly -> 3-part natural PK (ticker, period_end_date, filing_date).
    # Real constraint names confirmed live 2026-06-04.
    op.execute(
        "ALTER TABLE platform.fundamentals_quarterly "
        "ALTER COLUMN period_end_date SET NOT NULL"
    )
    op.execute(
        "ALTER TABLE platform.fundamentals_quarterly "
        "ALTER COLUMN filing_date SET NOT NULL"
    )
    op.execute(
        "ALTER TABLE platform.fundamentals_quarterly "
        "DROP CONSTRAINT IF EXISTS fundamentals_quarterly_pkey"
    )
    op.execute(
        "ALTER TABLE platform.fundamentals_quarterly "
        "DROP CONSTRAINT IF EXISTS uq_fundamentals_ticker_filing"
    )
    op.execute(
        "ALTER TABLE platform.fundamentals_quarterly "
        "ADD PRIMARY KEY (ticker, period_end_date, filing_date)"
    )
    # The surrogate ``id`` column is RETAINED as a plain column (Step-1 introspection
    # proved no FK references it). Not dropped — a later migration can if needed.

    # corporate_events.event_kind — NO-OP. The live CHECK already admits
    # delisting / bankruptcy_reorg / bankruptcy_liquidation (verified 2026-06-04).


def downgrade() -> None:
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ALTER COLUMN lifetime_start SET DEFAULT '1900-01-01'"
    )
    op.execute(
        "ALTER TABLE platform.fundamentals_quarterly "
        "DROP CONSTRAINT IF EXISTS fundamentals_quarterly_pkey"
    )
    op.execute(
        "ALTER TABLE platform.fundamentals_quarterly ADD PRIMARY KEY (id)"
    )
    op.execute(
        "ALTER TABLE platform.fundamentals_quarterly "
        "ADD CONSTRAINT uq_fundamentals_ticker_filing UNIQUE (ticker, filing_date)"
    )
