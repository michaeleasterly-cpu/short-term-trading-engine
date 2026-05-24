"""system-ops cleanup — DROP tax_lots + execution_quality_log.

Both tables were schema-only LIVE-gated stubs with no functional
consumer:

  - `platform.tax_lots`: writer (`tpcore/tax/lot_tracker.py:51`) and
    reader (`tpcore/tax/wash_sale.py:56`) were both TODO `NotImplementedError`
    stubs. No external importer of `lot_tracker.py`. The lot ledger
    will be re-designed if/when LIVE trading needs FIFO lot tracking.
  - `platform.execution_quality_log`: writer
    (`tpcore/quality/execution_quality.py:51`) wrote rows, but the
    only consumer was `scripts/audit_data_pipeline.py:1125` counting
    rows. Silent-write defect — keep the structlog fallback path
    (always-emit), drop the DB substrate.

Same-PR companion changes:
  - Delete `tpcore/tax/lot_tracker.py` (orphan TODO module).
  - Update `tpcore/tax/wash_sale.py` docstring + leave the class
    (still referenced by `tpcore/tax/loss_harvester.py`).
  - Simplify `tpcore/quality/execution_quality.py::ExecutionQualityWriter`
    to always use the structlog fallback (no DB branch).
  - Strip both names from
    `scripts/audit_data_pipeline.py::EXPECTED_EMPTY` and
    `platform/README.md`.

Downgrades recreate empty tables so a rollback doesn't break any
external script that expects the relations to exist; the data wasn't
populated to begin with.

Revision ID: 20260524_1900
Revises: 20260524_1800
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_1900"
down_revision: str | None = "20260524_1800"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform.tax_lots")
    op.execute("DROP TABLE IF EXISTS platform.execution_quality_log")


def downgrade() -> None:
    # Recreate skeletons (NOT full original schema — these were stubs
    # from `20260509_0000_initial_platform_schema.py`; the original
    # column list is preserved in that migration if a future
    # implementer wants to replay it).
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.tax_lots (
            lot_id            TEXT PRIMARY KEY,
            ticker            TEXT NOT NULL,
            engine_id         TEXT NOT NULL,
            acquisition_date  DATE NOT NULL,
            shares            NUMERIC NOT NULL,
            cost_basis        NUMERIC NOT NULL,
            status            TEXT NOT NULL DEFAULT 'open',
            closed_at         TIMESTAMPTZ,
            realized_pnl      NUMERIC
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.execution_quality_log (
            broker            TEXT NOT NULL,
            order_id          TEXT NOT NULL,
            requested_price   NUMERIC,
            fill_price        NUMERIC NOT NULL,
            slippage_bps      NUMERIC NOT NULL,
            partial_fill      BOOLEAN NOT NULL DEFAULT FALSE,
            paper_or_live     TEXT NOT NULL,
            timestamp         TIMESTAMPTZ NOT NULL,
            notes             TEXT,
            PRIMARY KEY (broker, order_id)
        )
        """
    )
