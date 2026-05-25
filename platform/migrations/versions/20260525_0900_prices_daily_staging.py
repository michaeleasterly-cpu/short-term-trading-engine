"""P3 trust-audit — ``platform.prices_daily_staging`` work table.

The 2026-05-25 P3 remediation: every batch destined for
``platform.prices_daily`` first lands in this staging table where
batch-level validation (row count vs archive, no in-batch
duplicates, source-priority sanity) runs BEFORE the rows reach
production. Failed validation leaves the staging rows for forensic
review and blocks the merge.

Schema mirrors ``prices_daily`` minus the FK on classification_id
(deferred to the production merge — staging is intentionally
relationship-free) plus three audit columns:

    staging_run_id   UUID NOT NULL — usually the ingest_manifest_id
                                     of the producing run; groups all
                                     rows that came in together.
    staged_at        TIMESTAMPTZ NOT NULL DEFAULT now()
    promoted         BOOLEAN NOT NULL DEFAULT false — set true after
                     the production merge succeeds; older completed
                     batches can be GC'd by a scheduled cleanup.

A composite PK on (staging_run_id, ticker, date) so the same batch
cannot stage duplicate rows for the same ticker/date — that would
be a producer bug, not legitimate input. Different batches can
share (ticker, date) — the run_id prefix prevents collision.

Per the operator's P3 scope: prices_daily ONLY. Other tables get
their own staging migrations if and when their handlers adopt this
contract.

Revision ID: 20260525_0900
Revises: 20260525_0700
Create Date: 2026-05-25
"""
from alembic import op

revision: str = "20260525_0900"
down_revision: str | None = "20260525_0700"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.prices_daily_staging (
            staging_run_id     UUID NOT NULL,
            ticker             TEXT NOT NULL,
            date               DATE NOT NULL,
            open               NUMERIC,
            high               NUMERIC,
            low                NUMERIC,
            close              NUMERIC,
            volume             BIGINT,
            adjusted_close     NUMERIC,
            delisted           BOOLEAN,
            delisting_date     DATE,
            source             TEXT,
            staged_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            promoted           BOOLEAN NOT NULL DEFAULT false,
            PRIMARY KEY (staging_run_id, ticker, date)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_prices_daily_staging_run "
        "ON platform.prices_daily_staging (staging_run_id) "
        "WHERE promoted = false"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_prices_daily_staging_staged_at "
        "ON platform.prices_daily_staging (staged_at) "
        "WHERE promoted = false"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform.prices_daily_staging")
