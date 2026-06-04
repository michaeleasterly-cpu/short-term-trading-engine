"""Plan 2 — demote earnings_events_count_snapshot to a VIEW (spec §2.4, OQ-4).

Live table columns (information_schema, 2026-06-04):
    ticker      text          NOT NULL  (PK)
    beat_count  bigint        NOT NULL
    snapshot_at timestamptz   NOT NULL

``beat_count`` is the per-ticker count of platform.earnings_events rows with
``event_type IN ('EARNINGS_BEAT','EARNINGS_NO_BEAT')`` (the full reported-earnings
population — see tpcore/quality/validation/checks/earnings_events_monotone.py
``_LIVE_COUNTS_SQL``). The VIEW reproduces exactly those three columns so plain
SELECT readers do not break.

⚠️ CONCERN (surfaced to the coordinator — see PR body / agent report):
``earnings_events_monotone`` does NOT merely READ this table — it maintains it as
a durable MUTABLE baseline: ``SELECT ... FOR UPDATE`` of the prior counts then
``INSERT ... ON CONFLICT (ticker) DO UPDATE`` of the new counts, all in one
transaction. A VIEW is (a) not updatable (FOR UPDATE / UPSERT will error) and
(b) always equal to the live count, which silently defeats the monotone-shrink
invariant (prior == current ⇒ no shrink ever detected). This migration is
authored per the plan's exact §2.4 instruction, but applying it requires the
monotone check's baseline writer to be retargeted FIRST (the OQ-4 fold to
data_quality_log ``kind='count_snapshot'`` OR a dedicated baseline table). Do
NOT apply 0400 before that is resolved.
"""
from __future__ import annotations

from alembic import op

revision = "20260604_0400"
down_revision = "20260604_0300"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform.earnings_events_count_snapshot CASCADE")
    # Reproduce the dropped table's exact columns (ticker, beat_count, snapshot_at)
    # as a live VIEW over earnings_events. snapshot_at is the latest ingest time
    # for the ticker's reported-earnings rows (best-effort analog of the former
    # baseline-refresh timestamp).
    op.execute(
        """
        CREATE OR REPLACE VIEW platform.earnings_events_count_snapshot AS
        SELECT
            ticker,
            COUNT(*)::bigint                                   AS beat_count,
            COALESCE(MAX(recorded_at), now())                 AS snapshot_at
        FROM platform.earnings_events
        WHERE event_type IN ('EARNINGS_BEAT', 'EARNINGS_NO_BEAT')
        GROUP BY ticker
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS platform.earnings_events_count_snapshot")
