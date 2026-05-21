"""ingestion_metrics — durable per-source ingest-run metrics for the D2
shrinkage detector (LOCKED 2026-05-18 archive-substrate migration).

The R3 substrate (S3 object storage, PR #235) ships the local-FS-bound
``data/<source>_archive/`` to an attached bucket so the recovery path
survives Railway's ephemeral filesystem. D2 is the still-PENDING half:
the DETECTION path currently reads the SINGLE-PRIOR CSV from local FS
via ``csv_archive.detect_shrinkage`` → dies on Railway ephemeral FS,
also poisoned by single-prior-baseline noise on Mac.

The LOCKED design: persist per-source row-count + date-range + coverage
to a durable Postgres table on every ingest, and gate shrinkage on
deviation vs the ROLLING MEDIAN of recent history (not single-prior).
This table is the durable substrate. Detection no longer cares about
the local FS at all; the R3 bucket is for archive recovery only.

Shape:

* PRIMARY KEY ``(source, ingested_at)`` — one row per ingest run per
  source. Naturally append-only (the rolling-median check reads recent
  rows for a source; old rows accumulate but the per-source partition
  is tiny — one row per scheduled refresh per source). No vacuum
  pressure on the platform.* schema.
* ``row_count BIGINT`` — what the ingest landed at the producer's
  archive layer; the rolling-median compares THIS column across runs.
  CHECK ``row_count >= 0``.
* ``min_date DATE`` / ``max_date DATE`` — the data window the run
  covered (nullable: not every source has a date-keyed shape — e.g.
  fundamentals is filing-keyed; nullable lets us extend later without
  a backfill migration).
* ``coverage_pct NUMERIC`` — producer-self-reported coverage fraction
  (0..1). Optional / nullable for sources that don't expose a coverage
  number (the daily_bars stage exposes one; the FRED single-series
  pulls don't have a meaningful one).

Compared to the v1 single-prior-CSV ``detect_shrinkage``:
* RobUSTNESS: a one-off short run (vendor blip) doesn't poison the
  baseline — the median absorbs a single outlier.
* SUBSTRATE-NEUTRAL: lives in Postgres, survives Railway's ephemeral
  FS, accessible from any deploy of the platform regardless of
  archive-bucket configuration.
* SEPARABILITY: the R3 substrate (S3) is now ONLY for recovery, never
  detection — the operator's separability principle from the locked
  2026-05-18 design.

Concurrent operation: v1 single-prior-CSV ``detect_shrinkage`` STAYS
IN PLACE for this PR. Both detectors run in parallel and disagree
events emit ``SHRINKAGE_DETECTORS_DISAGREE`` for forensic visibility.
A v2 PR retires the old detector after a soak period (defined by the
operator, not this PR).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260521_0000"
down_revision: str | None = "20260520_0200"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingestion_metrics",
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "ingested_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("min_date", sa.Date(), nullable=True),
        sa.Column("max_date", sa.Date(), nullable=True),
        sa.Column("coverage_pct", sa.Numeric(6, 4), nullable=True),
        sa.PrimaryKeyConstraint(
            "source", "ingested_at",
            name="ingestion_metrics_pk",
        ),
        sa.CheckConstraint(
            "row_count >= 0",
            name="ingestion_metrics_row_count_nonneg_chk",
        ),
        sa.CheckConstraint(
            "coverage_pct IS NULL OR (coverage_pct >= 0 AND coverage_pct <= 1)",
            name="ingestion_metrics_coverage_pct_range_chk",
        ),
        sa.CheckConstraint(
            "min_date IS NULL OR max_date IS NULL OR max_date >= min_date",
            name="ingestion_metrics_date_range_chk",
        ),
        schema="platform",
    )
    # Recent-history index — the rolling-median query reads
    # ``ORDER BY ingested_at DESC LIMIT N`` per source. The PK
    # already covers (source, ingested_at) so a separate index is
    # only needed if the median query starts filtering on date
    # ranges; deferring to v2.
    op.execute(
        "COMMENT ON TABLE platform.ingestion_metrics IS "
        "'Durable per-source ingest-run metrics — substrate for the "
        "D2 rolling-median shrinkage detector (LOCKED 2026-05-18 "
        "archive-substrate migration). Replaces the local-FS-bound "
        "single-prior-CSV detector in tpcore.ingestion.csv_archive "
        "for shrinkage detection only; the R3 substrate (S3 bucket) "
        "stays the recovery substrate. Row count grows by ~one row "
        "per source per scheduled refresh.'"
    )


def downgrade() -> None:
    op.drop_table("ingestion_metrics", schema="platform")
