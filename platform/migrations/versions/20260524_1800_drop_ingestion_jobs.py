"""system-ops cleanup — DROP TABLE platform.ingestion_jobs.

The legacy Railway-tick `IngestionEngine` dispatcher (deleted same
PR: `tpcore/ingestion/engine.py`, `tpcore/tests/test_ingestion_engine.py`,
`ops/ingestion_engine.py`) used this table as its bookkeeping
substrate. The deterministic-cascade architecture + `application_log`
event bus replaced that loop 2026-05-12; the table has been FROZEN per
operator memory ever since (`project_railway_hobby_tier.md`:
"platform.ingestion_jobs is FROZEN — application_log INGESTION_COMPLETE
wins").

The 4 surviving rows held seed/config for `fundamentals_refresh`,
`data_validation`, `daily_bars`, and `corporate_actions`. The only
production consumer was `_load_daily_bars_config` in `scripts/ops.py`,
which read the `daily_bars.config` blob. That config (universe,
batch_size, lookback_days, min_price, min_volume, inter_batch_sleep_sec)
is now inlined in `scripts/ops.py` — it never changed in production
after the table was frozen.

Architect verdict (db-architect 2026-05-24): "highest value because
it is the only table both provably dead AND still actively confusing
(every audit script special-cases it)".

Same-PR companion changes:
  - Drop legacy IngestionEngine dispatcher class + its test + the
    ops/ingestion_engine.py daemon wrapper.
  - Strip ingestion_jobs references from scripts/audit_data_pipeline.py
    + scripts/audit_all_tables.py + tpcore/ladder/disposition.py
    + tpcore/audit/cross_table.py docstring + tpcore/ingestion/__init__.py
    + ops/__init__.py.
  - Inline the daily_bars config in scripts/ops.py:_load_daily_bars_config.

The downgrade re-creates the table empty so a rollback doesn't break
any rare external script that still expects the relation to exist;
the data isn't recoverable (it was a Railway-era snapshot, not a
durable record).

Revision ID: 20260524_1800
Revises: 20260524_1702
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_1800"
down_revision: str | None = "20260524_1702"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform.ingestion_jobs")


def downgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.ingestion_jobs (
            job_name          TEXT PRIMARY KEY,
            schedule          TEXT NOT NULL,
            provider          TEXT NOT NULL,
            config            JSONB NOT NULL DEFAULT '{}'::jsonb,
            enabled           BOOLEAN NOT NULL DEFAULT TRUE,
            next_run          TIMESTAMPTZ,
            last_run_at       TIMESTAMPTZ,
            last_status       TEXT,
            last_error        TEXT,
            last_duration_ms  INTEGER,
            recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
