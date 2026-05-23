"""v2.2 P6 — orphan-bearing child tables: classification_id + FK + per-table orphan disposition.

Batch migration covering 6 child tables with non-zero orphan counts.
Per spec §1.11 orphan-protocol-per-table matrix:

  Table                   Rows      Orphans  Path
  --------------------    -------   -------  --------------------------------
  short_interest            4,553         3  A (BACKFILL via parent_resolver)
  liquidity_tiers           7,692         8  B (DELETE — derived; rebuildable)
  earnings_events          35,074        12  A (BACKFILL)
  spread_observations      31,900        33  B (DELETE — derived; rebuildable)
  fundamentals_quarterly  178,902       135  A (BACKFILL)
  corporate_actions       111,726     1,506  A (BACKFILL)

Path A (BACKFILL): leave classification_id NULL for orphan rows in this
migration. A later parent_resolver run (FMP /profile + OpenFIGI + SEC
EDGAR per per-handler-lane dispatch) creates the missing parent
classifications and fills the NULL classification_id values. The FK
NOT VALID + VALIDATE here works because NULL columns ignore FK checks
by default. Operator can flip these tables' classification_id to NOT NULL
in a follow-up migration once the orphan parents are backfilled.

Path B (DELETE): orphan rows are derived data (engine output / cleanup
substrate); rebuildable on next engine run. DELETE them in-migration,
then NOT NULL flip.

Per-table sequence:
  1. ADD COLUMN classification_id text (nullable)
  2. UPDATE backfill via JOIN on current_ticker active-status parents
  3. Path B only: DELETE orphans WHERE classification_id IS NULL
  4. Path B only: ALTER COLUMN SET NOT NULL
  5. ADD FK NOT VALID
  6. VALIDATE CONSTRAINT (under SET LOCAL statement_timeout='30min')
  7. Index on classification_id

prices_daily INTENTIONALLY excluded — 21M rows + 4.3 GB warrant a
dedicated migration with extra care (and chunked backfill if needed).

Revision ID: 20260524_0600
Revises: 20260524_0500
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_0600"
down_revision: str | None = "20260524_0500"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


# (table_name, path) — Path A leaves NULL classification_id for orphans; Path B deletes orphans.
_TABLES_AND_PATHS: tuple[tuple[str, str], ...] = (
    ("short_interest", "A"),
    ("liquidity_tiers", "B"),
    ("earnings_events", "A"),
    ("spread_observations", "B"),
    ("fundamentals_quarterly", "A"),
    ("corporate_actions", "A"),
)


def upgrade() -> None:
    for table, path in _TABLES_AND_PATHS:
        fk_name = f"{table}_classification_id_fk"
        idx_name = f"{table}_classification_id_idx"

        op.execute(
            f"ALTER TABLE platform.{table} ADD COLUMN IF NOT EXISTS classification_id text"
        )
        op.execute(
            f"""
            UPDATE platform.{table} t
            SET classification_id = tc.id
            FROM platform.ticker_classifications tc
            WHERE t.ticker = tc.current_ticker
              AND tc.status IN ('active', 'active_when_issued')
              AND t.classification_id IS NULL
            """
        )

        if path == "B":
            # DELETE orphans (derived data, rebuildable)
            op.execute(
                f"DELETE FROM platform.{table} WHERE classification_id IS NULL"
            )
            op.execute(
                f"ALTER TABLE platform.{table} ALTER COLUMN classification_id SET NOT NULL"
            )
        # Path A: leave NULL — parent_resolver fills later.

        op.execute(
            f"""
            DO $$
            BEGIN
                ALTER TABLE platform.{table}
                    ADD CONSTRAINT {fk_name}
                    FOREIGN KEY (classification_id) REFERENCES platform.ticker_classifications(id)
                    ON UPDATE CASCADE ON DELETE RESTRICT NOT VALID;
            EXCEPTION
                WHEN duplicate_object THEN NULL;
            END $$
            """
        )
        op.execute("SET LOCAL statement_timeout = '30min'")
        op.execute(
            f"ALTER TABLE platform.{table} VALIDATE CONSTRAINT {fk_name}"
        )
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {idx_name} ON platform.{table} (classification_id)"
        )


def downgrade() -> None:
    for table, _path in reversed(_TABLES_AND_PATHS):
        fk_name = f"{table}_classification_id_fk"
        idx_name = f"{table}_classification_id_idx"
        op.execute(f"DROP INDEX IF EXISTS platform.{idx_name}")
        op.execute(
            f"ALTER TABLE platform.{table} DROP CONSTRAINT IF EXISTS {fk_name}"
        )
        op.execute(
            f"ALTER TABLE platform.{table} DROP COLUMN IF EXISTS classification_id"
        )
        # Path B deletes are deliberately NOT restored — derived data,
        # next engine run rebuilds them.
