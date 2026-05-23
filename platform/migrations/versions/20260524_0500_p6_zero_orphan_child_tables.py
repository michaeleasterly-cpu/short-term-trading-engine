"""v2.2 P6 — zero-orphan child tables: classification_id + FK to ticker_classifications.

Batch migration covering 6 child tables whose v2.1 Phase 0 audit showed
ZERO orphans against ticker_classifications. No DELETE needed; straight
column-add + backfill + FK NOT VALID + VALIDATE pattern.

Tables (verified 2026-05-23 — 0 orphans each against active-status parents):
  insider_transactions      647,163 rows
  sec_material_events       237,767 rows
  borrow_rates                   33 rows
  social_sentiment            1,355 rows
  options_max_pain                1 row    (uses `symbol` column, not `ticker`)
  insider_sentiment             520 rows   (uses `symbol` column)

Per-table sequence:
  1. ADD COLUMN classification_id text (nullable initially)
  2. UPDATE backfill via JOIN on current_ticker = `ticker` OR `symbol`
  3. ALTER COLUMN SET NOT NULL (safe — 0 orphans verified)
  4. ADD CONSTRAINT FK NOT VALID with ON UPDATE CASCADE ON DELETE RESTRICT
  5. SET LOCAL statement_timeout = '30min'
  6. VALIDATE CONSTRAINT
  7. CREATE INDEX on classification_id for join performance

Disk impact estimate: ~14 bytes/row × ~900K rows total + 6 indexes
≈ 30 MB. Well within Supabase Pro 8GB headroom; operator-cognizant.

prices_daily is INTENTIONALLY excluded — its 21M rows + 4.3GB current
footprint warrants its own migration with extra care (separate file,
TBD). Same for the orphan-bearing tables (universe_candidates already
done in 20260524_0400; short_interest / liquidity_tiers / earnings_events /
spread_observations / fundamentals_quarterly / corporate_actions get a
separate migration that handles their orphan-disposition per spec §1.11).

Revision ID: 20260524_0500
Revises: 20260524_0400
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_0500"
down_revision: str | None = "20260524_0400"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


# (table_name, ticker_column_name)
_ZERO_ORPHAN_TABLES: tuple[tuple[str, str], ...] = (
    ("insider_transactions", "ticker"),
    ("sec_material_events", "ticker"),
    ("borrow_rates", "ticker"),
    ("social_sentiment", "ticker"),
    ("options_max_pain", "symbol"),
    ("insider_sentiment", "symbol"),
)


def upgrade() -> None:
    for table, col in _ZERO_ORPHAN_TABLES:
        fk_name = f"{table}_classification_id_fk"
        idx_name = f"{table}_classification_id_idx"

        # 1. Add the column.
        op.execute(
            f"ALTER TABLE platform.{table} ADD COLUMN IF NOT EXISTS classification_id text"
        )

        # 2. Backfill via JOIN.
        op.execute(
            f"""
            UPDATE platform.{table} t
            SET classification_id = tc.id
            FROM platform.ticker_classifications tc
            WHERE t.{col} = tc.current_ticker
              AND tc.status IN ('active', 'active_when_issued')
              AND t.classification_id IS NULL
            """
        )

        # 3. NOT NULL flip — safe because audit confirmed 0 orphans.
        op.execute(
            f"ALTER TABLE platform.{table} ALTER COLUMN classification_id SET NOT NULL"
        )

        # 4. FK constraint NOT VALID. Wrapped in DO $$ for partial-replay
        # idempotency.
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

        # 5 + 6. VALIDATE under raised statement_timeout (separate execute
        # because asyncpg refuses multi-statement prepared statements).
        op.execute("SET LOCAL statement_timeout = '30min'")
        op.execute(
            f"ALTER TABLE platform.{table} VALIDATE CONSTRAINT {fk_name}"
        )

        # 7. Index on FK column for join perf (Postgres doesn't auto-index FKs).
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {idx_name} ON platform.{table} (classification_id)"
        )


def downgrade() -> None:
    for table, _col in reversed(_ZERO_ORPHAN_TABLES):
        fk_name = f"{table}_classification_id_fk"
        idx_name = f"{table}_classification_id_idx"
        op.execute(f"DROP INDEX IF EXISTS platform.{idx_name}")
        op.execute(
            f"ALTER TABLE platform.{table} DROP CONSTRAINT IF EXISTS {fk_name}"
        )
        op.execute(
            f"ALTER TABLE platform.{table} DROP COLUMN IF EXISTS classification_id"
        )
