"""P0_5 trust-audit — freeze ``source='tradier'`` on ``platform.prices_daily``.

The 2026-05-25 data session named this as one of the P0 follow-ons.
Tradier was a historical daily-bars feed; the operator memory
``project_fmp_primary_daily_bars_2026_05_22`` documents the switch
to FMP as primary on 2026-05-22. Live state at 2026-05-25:
**15,075,210 source='tradier' rows in prices_daily**, latest bar
2026-05-22 (25 days stale — Tradier API key revoked / unused).

This migration adds a CHECK constraint forbidding NEW
``source='tradier'`` writes (the 15M existing rows are preserved
unchanged as historical-only attribution). NOT VALID skips the
one-time validation pass of the existing 21M-row table; future
INSERTs and UPDATEs that would set ``source='tradier'`` are
rejected by Postgres at constraint-check time.

Layered defence
---------------

This is the third layer of provenance protection on prices_daily:

  1. ``platform._source_priority(s)`` (migration ``20260525_0700``)
     — ranks tradier=1 (lowest). The P4 provenance-downgrade guard
     on ``_upsert_bars`` / ``stage_then_promote_bars`` blocks
     tradier from OVERWRITING any existing row tagged with a
     higher-priority source. But that guard alone allows brand-new
     ``(ticker, date)`` pairs to land with ``source='tradier'``.
  2. ``platform.prices_daily_staging`` (migration ``20260525_0900``)
     — every batch lands here first; validation runs before
     promote. A bug in a re-introduced Tradier writer would surface
     in staging.
  3. THIS CHECK — Postgres flat-out rejects ``source='tradier'``
     INSERTs / UPDATEs. Defense-in-depth: even if the application
     layer regresses, the substrate refuses.

Revision ID: 20260525_1100
Revises: 20260525_0900
Create Date: 2026-05-25
"""
from alembic import op

revision: str = "20260525_1100"
down_revision: str | None = "20260525_0900"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # NOT VALID: only enforces on new INSERTs / UPDATEs that mutate
    # source. The 15M existing source='tradier' rows are NOT scanned
    # for compliance (the constraint name explicitly says
    # ``_no_new_tradier`` so operators reading the constraint can
    # tell it's a forward-only gate).
    op.execute(
        """
        ALTER TABLE platform.prices_daily
        ADD CONSTRAINT prices_daily_no_new_tradier
        CHECK (source <> 'tradier') NOT VALID
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE platform.prices_daily
        DROP CONSTRAINT IF EXISTS prices_daily_no_new_tradier
        """
    )
