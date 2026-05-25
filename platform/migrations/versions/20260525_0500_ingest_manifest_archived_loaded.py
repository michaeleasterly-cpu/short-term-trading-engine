"""Extend ``platform.ingest_manifest.status`` CHECK to include the
P1 trust-audit lifecycle states: ``archived`` + ``loaded``.

The original constraint (migration ``20260525_0200``) allowed
``('ok', 'partial', 'failed')`` — a 3-value placeholder for the
single-shot post-hoc reconciliation pattern. The P1 trust-audit
(2026-05-25) requires the manifest to track the explicit
archive-first lifecycle, with the archive landing as a separate
event from the production write:

    archived   — archive CSV is on disk, checksum recorded, no
                 production write has started for this batch
    loaded     — archive on disk + production write succeeded
    failed     — archive on disk + production write aborted
                 (the archive is preserved for forensics)

The legacy ``ok`` / ``partial`` values are retained for
backward-compatibility with any future writer that wants the
single-shot semantics; they aren't written by the new
``tpcore.ingestion.manifest`` module (which uses only
archived/loaded/failed).

Revision ID: 20260525_0500
Revises: 20260525_0200
Create Date: 2026-05-25
"""
from alembic import op

revision: str = "20260525_0500"
# Rebased after 20260524_2000 (engine_abstraction_universe_view, also a
# child of 20260525_0200 — the multi-head collapse).
down_revision: str | None = "20260524_2000"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE platform.ingest_manifest
        DROP CONSTRAINT IF EXISTS ingest_manifest_status_check
        """
    )
    op.execute(
        """
        ALTER TABLE platform.ingest_manifest
        ADD CONSTRAINT ingest_manifest_status_check
        CHECK (status IN ('ok', 'partial', 'failed', 'archived', 'loaded'))
        """
    )


def downgrade() -> None:
    # NB: a manifest carrying status='archived' or 'loaded' will block
    # the downgrade. Operator must scrub those rows first (they're
    # historical evidence; deletion is a documented operator action
    # per the rebuild posture).
    op.execute(
        """
        ALTER TABLE platform.ingest_manifest
        DROP CONSTRAINT IF EXISTS ingest_manifest_status_check
        """
    )
    op.execute(
        """
        ALTER TABLE platform.ingest_manifest
        ADD CONSTRAINT ingest_manifest_status_check
        CHECK (status IN ('ok', 'partial', 'failed'))
        """
    )
