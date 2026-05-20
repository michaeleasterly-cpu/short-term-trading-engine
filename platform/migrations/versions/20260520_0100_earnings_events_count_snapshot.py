"""earnings_events_count_snapshot — per-ticker EARNINGS_BEAT row-count
baseline for the zero-tolerance monotone-non-decrease invariant on
platform.earnings_events.

EARNINGS_BEAT rows are append-only historical events — a 2023 Q2 beat
does NOT unhappen. The monotone invariant (``earnings_events_monotone``)
therefore demands that for every ticker the live BEAT ``COUNT(*)``
across runs is non-decreasing. To compare across runs we need a durable
per-ticker baseline; this table is it.

Shape (mirrors ``sec_insider_row_counts_snapshot``):

* PRIMARY KEY (ticker) — one row per ticker, NOT a history table.
  Each run UPSERTs the current count after a successful compare; the
  next run gates against THAT.
* ``beat_count BIGINT`` — number of EARNINGS_BEAT rows in
  ``platform.earnings_events`` for the ticker.
* ``snapshot_at TIMESTAMPTZ`` — debugging aid (when was this baseline
  last touched), NOT a comparison key.

The check itself runs the read + compare + UPSERT in a single
transaction so a crash mid-update can't poison the next cycle's
baseline.

KNOWN GAP — caveated explicitly (P1 follow-on):
``scripts/backfill_earnings_events.py::_classify_beat`` only emits a
row when ``actual_eps > estimated_eps × 1.05``. MISS / IN-LINE
earnings produce NO ROW. So this per-ticker monotone-non-decrease
invariant catches VENDOR TRUNCATION (a re-ingest that drops historical
beats), but it does NOT catch a MISSED DETECTION from an FMP outage
that would have written a beat row had the feed responded. The honest
fix is to emit a ``NO_BEAT`` sentinel per quarter so per-quarter
completeness becomes auditable — tracked as a P1 follow-on under the
"Autonomous self-heal" section of TODO.md.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260520_0100"
down_revision: str | None = "20260520_0000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "earnings_events_count_snapshot",
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("beat_count", sa.BigInteger(), nullable=False),
        sa.Column(
            "snapshot_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "ticker", name="earnings_events_count_snapshot_pk"
        ),
        sa.CheckConstraint(
            "beat_count >= 0",
            name="earnings_events_count_snapshot_nonneg_chk",
        ),
        schema="platform",
    )
    op.execute(
        "COMMENT ON TABLE platform.earnings_events_count_snapshot IS "
        "'Per-ticker EARNINGS_BEAT row-count baseline. Gates the "
        "zero-tolerance earnings_events_monotone invariant: every "
        "ticker''s live COUNT(*) WHERE event_type=''EARNINGS_BEAT'' "
        "must be >= the value here. EARNINGS_BEAT rows are append-only; "
        "any per-ticker decrease is vendor truncation / deletion event. "
        "KNOWN GAP (P1 follow-on): backfill_earnings_events._classify_beat "
        "is BEAT-only (actual > estimate * 1.05) — this invariant catches "
        "truncation, NOT missed-detection from an FMP outage. Resolution "
        "requires emitting a NO_BEAT sentinel per quarter so per-quarter "
        "completeness becomes auditable.'"
    )


def downgrade() -> None:
    op.drop_table(
        "earnings_events_count_snapshot", schema="platform"
    )
