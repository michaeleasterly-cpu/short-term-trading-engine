"""Add platform.ingest_manifest + platform.ingest_quarantine for the
2026-05-25 acceptance audit's P4 (source reconciliation) and P5
(failed-record retention) fixes.

P4 — ingest_manifest:
    Every ingest batch writes one row recording source, provider,
    pulled_at, source_locator (URL / file path / API endpoint),
    expected/actual row counts, status, and checksum/hash when
    available. Enables reconciliation queries comparing source
    counts to DB counts.

P5 — ingest_quarantine:
    Failed records (parse error, validation rejection, FK violation
    on insert) are persisted here instead of silently dropped. Each
    row carries the raw payload, the error, and a retry timeline.
    Solves the audit finding: "no quarantine substrate exists; bad
    records are not persisted for inspection".

Both tables are append-only operational substrates. They do NOT
participate in the v2.2 classification_id FK chain (manifest and
quarantine are about INGEST events, not domain rows).

Revision ID: 20260525_0200
Revises: 20260525_0100
Create Date: 2026-05-25
"""
from alembic import op

revision: str = "20260525_0200"
down_revision: str | None = "20260525_0100"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # P4: ingest_manifest
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.ingest_manifest (
            manifest_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source             TEXT NOT NULL,
            provider           TEXT NOT NULL,
            pulled_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            source_locator     TEXT NOT NULL,
            expected_rows      BIGINT,
            actual_rows        BIGINT NOT NULL,
            status             TEXT NOT NULL CHECK (status IN ('ok', 'partial', 'failed')),
            checksum           TEXT,
            date_range_start   DATE,
            date_range_end     DATE,
            notes              TEXT,
            recorded_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ingest_manifest_source_pulled_at "
        "ON platform.ingest_manifest (source, pulled_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ingest_manifest_status "
        "ON platform.ingest_manifest (status) WHERE status <> 'ok'"
    )

    # P5: ingest_quarantine
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.ingest_quarantine (
            quarantine_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source             TEXT NOT NULL,
            target_table       TEXT NOT NULL,
            payload            JSONB NOT NULL,
            error_message      TEXT NOT NULL,
            error_kind         TEXT NOT NULL CHECK (error_kind IN (
                'parse', 'validation', 'fk_violation', 'unique_violation',
                'check_violation', 'type_coercion', 'other'
            )),
            rejected_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            retry_count        INT NOT NULL DEFAULT 0,
            retry_status       TEXT NOT NULL DEFAULT 'pending' CHECK (retry_status IN (
                'pending', 'retried_ok', 'retried_failed', 'abandoned'
            )),
            manifest_id        UUID REFERENCES platform.ingest_manifest(manifest_id),
            recorded_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ingest_quarantine_source_rejected_at "
        "ON platform.ingest_quarantine (source, rejected_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ingest_quarantine_retry_pending "
        "ON platform.ingest_quarantine (retry_status, rejected_at) "
        "WHERE retry_status = 'pending'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform.ingest_quarantine")
    op.execute("DROP TABLE IF EXISTS platform.ingest_manifest")
