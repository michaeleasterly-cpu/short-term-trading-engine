"""v2.2 Phase P2 — create platform.ticker_history (load-bearing for ticker-at-row-date).

Per v2.2 spec §1.7. Every child-table INSERT (live or backfill) looks up
`ticker_history` to get the ticker that was visible AT THE DATE the row represents.
The naive pattern of using `current_ticker` for backfill is wrong; a 2010 prices_daily
backfill for Meta today must write `ticker='FB'`, not `ticker='META'`.

Schema:
- `classification_id text` — FK to ticker_classifications(id); cannot reference yet
  because ticker_classifications.id is still nullable post-P2. The FK is added in
  v2.2 P5 after the PK swap.
- `ticker text` — the symbol that was visible during the validity window.
- `valid_from date` — inclusive start of the validity window.
- `valid_to date NULL` — exclusive end; NULL means current.
- PRIMARY KEY (classification_id, valid_from) — one row per rename event per security.
- EXCLUDE constraint — no overlapping validity windows for the same classification_id.

The EXCLUDE constraint requires the `btree_gist` extension (provided by Supabase Pro;
already used elsewhere). The migration installs it if missing (no-op on existing install).

Population pattern (handled in v2.2 P4 by parent_resolver, NOT in this migration):
- First-seen: `(classification_id, ticker, valid_from=today, valid_to=NULL)`.
- Rename: `UPDATE ... SET valid_to=yesterday WHERE valid_to IS NULL`; then INSERT new row.

Pre-flight gates (per v2.2 plan P2):
- 20260524_0000 (ticker_classifications column adds) MUST be applied first.
- btree_gist installable (Supabase Pro: yes; verify via Phase 0 audit).

Revision ID: 20260524_0100
Revises: 20260524_0000
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_0100"
down_revision: str | None = "20260524_0000"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # ── 1. Ensure btree_gist extension is available ──────────────────
    # Required for the EXCLUDE USING gist no-overlap constraint.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    # ── 2. Create ticker_history table ───────────────────────────────
    # Note: FK to ticker_classifications(id) NOT added here — id is still nullable
    # post-P2. The FK is added in P5 after the PK swap. Until then, callers
    # must populate classification_id with valid TKR-14 values manually.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.ticker_history (
            classification_id text NOT NULL,
            ticker            text NOT NULL,
            valid_from        date NOT NULL,
            valid_to          date,
            PRIMARY KEY (classification_id, valid_from)
        )
        """
    )

    # ── 3. EXCLUDE no-overlap constraint ─────────────────────────────
    # Prevents two rows for the same classification_id with overlapping
    # [valid_from, valid_to) windows. Catches bad data at INSERT time, not after.
    op.execute(
        """
        ALTER TABLE platform.ticker_history
            ADD CONSTRAINT ticker_history_no_overlap
            EXCLUDE USING gist (
                classification_id WITH =,
                daterange(valid_from, COALESCE(valid_to, 'infinity'::date), '[)') WITH &&
            )
        """
    )

    # ── 4. Index for active-ticker lookups (the hot read path) ───────
    # Used by parent_resolver's "is this ticker currently registered?" check.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ticker_history_ticker_active_idx
            ON platform.ticker_history (ticker)
            WHERE valid_to IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS platform.ticker_history_ticker_active_idx")
    op.execute(
        "ALTER TABLE platform.ticker_history DROP CONSTRAINT IF EXISTS ticker_history_no_overlap"
    )
    op.execute("DROP TABLE IF EXISTS platform.ticker_history")
    # Note: do NOT drop btree_gist; other constraints may rely on it.
