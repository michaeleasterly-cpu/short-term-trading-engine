"""ticker_classifications_source_count — write-time-recorded source-of-
truth row count baseline for the zero-tolerance drift invariant on
platform.ticker_classifications.

``platform.ticker_classifications`` is UPSTREAM-DERIVED from Alpaca's
``/v2/assets`` listing — Alpaca IS the asset-class source of truth that
defines what counts as "stock" vs "etf" vs "spac" vs "fund". A B-shaped
"active universe survives the cut" invariant (the liquidity_tiers
shape) would be circular here because ticker_classifications IS the
universe definition.

The correct invariant for an upstream-derived table is row-count-
equals-source at write time: when the classifier ran, Alpaca returned
N assets and our table has N rows for that snapshot. ANY drift between
two refreshes (the live live ``COUNT(*)`` not equaling the most recent
snapshot's ``source_count``) is a FAIL — zero tolerance, no percentage
knob (replaces the previous ``MIN_COVERAGE_PCT=0.90`` coverage knob).

Shape:

* PRIMARY KEY ``snapshot_at TIMESTAMPTZ DEFAULT now()`` — one row PER
  REFRESH (this IS a history table, deliberately, unlike the per-
  ticker monotone snapshots). Footprint is tiny (~12 rows/year on the
  monthly classify_tickers cadence) and the history is useful for
  triage / drift forensics.
* ``source_count BIGINT`` — Alpaca returned this many assets on this
  refresh. CHECK ``source_count > 0`` (a zero-row Alpaca response is
  itself a vendor failure that we want to fail hard on).

The classify_tickers script writes the snapshot in the SAME transaction
as the classifications upserts so a partial write can't poison the next
check's view of the source-of-truth count.

First-run behavior (snapshot table empty) returns PASS + a notice — the
next classify_tickers run seeds the baseline. Same "first-run seed"
pattern as the per-ticker monotone checks (sec_insider_monotone,
earnings_events_monotone).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260520_0200"
down_revision: str | None = "20260520_0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ticker_classifications_source_count",
        sa.Column(
            "snapshot_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("source_count", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "snapshot_at",
            name="ticker_classifications_source_count_pk",
        ),
        sa.CheckConstraint(
            "source_count > 0",
            name="ticker_classifications_source_count_positive_chk",
        ),
        schema="platform",
    )
    op.execute(
        "COMMENT ON TABLE platform.ticker_classifications_source_count IS "
        "'Per-refresh source-of-truth row count baseline for "
        "platform.ticker_classifications (Alpaca /v2/assets is the "
        "upstream). Gates the zero-tolerance "
        "ticker_classifications_coverage invariant: live COUNT(*) must "
        "equal the most recent snapshot''s source_count. ANY drift = "
        "FAIL. History table (one row per classify_tickers run); written "
        "atomically with the classification upserts. Replaces the "
        "previous percentage-knob coverage gate (MIN_COVERAGE_PCT=0.90) "
        "with a write-time-recorded source-of-truth invariant — the "
        "correct shape for an upstream-derived table.'"
    )


def downgrade() -> None:
    op.drop_table(
        "ticker_classifications_source_count", schema="platform"
    )
