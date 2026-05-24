"""v2.2 follow-on — swap ticker_classifications PK from (ticker) to (id).

Per the v2.2 design intent: TKR-14 `id` is the canonical stable identity;
`ticker` is mutable (re-IPOs, name changes, ticker-rotation events). The
PK should reflect what's stable. The 14 cross-table FKs already target
`id` — what's been holding back the PK swap is just that nobody flipped
the constraint until now.

Pre-conditions (audited 2026-05-24):
  - id IS NOT NULL on all 13,761 rows
  - ticker IS NOT NULL on all 13,761 rows
  - id has UNIQUE constraint `ticker_classifications_id_uniq` (becomes
    redundant after the PK swap — the new PK provides its own UNIQUE)
  - 0 FKs reference (ticker); 14 FKs reference (id) — v2.2 P9 (just shipped)
    dropped the last legacy ticker-keyed FKs

Migration shape (atomic in one alembic transaction):
  1. ALTER COLUMN ticker SET NOT NULL — make explicit (without this, the
     DROP CONSTRAINT pkey step also drops the IMPLICIT NOT NULL on
     ticker that came from being a PK column).
  2. DROP CONSTRAINT ticker_classifications_pkey — old PK on (ticker).
  3. ADD CONSTRAINT ticker_classifications_pkey PRIMARY KEY (id) — the
     new PK. PostgreSQL creates a new implicit UNIQUE index named
     ticker_classifications_pkey backing the constraint.
  4. ADD CONSTRAINT ticker_classifications_ticker_key UNIQUE (ticker) —
     preserves uniqueness invariant on ticker so existing
     `ON CONFLICT (ticker) DO UPDATE` UPSERT paths keep working.

The existing `ticker_classifications_id_uniq` UNIQUE constraint on id is
DELIBERATELY LEFT IN PLACE. Initial attempt dropped it; PostgreSQL refused
because the 14 cross-table FKs (`<child>_classification_id_fk`) are bound
to that specific unique index, not to "any unique on id". Dropping would
require CASCADE — which would orphan the FKs. Keeping the redundant
UNIQUE costs ~10 KB of index overhead (id values are 14-char text, 13,761
rows) and leaves the FK chain intact. The new PK provides PK semantics;
the redundant UNIQUE keeps the FK plumbing working without restructuring.

Consumer impact: ZERO read-path changes.
  - SELECT WHERE ticker = ... still uses an index (UNIQUE index on ticker
    replaces the PK index).
  - SELECT WHERE id = ... uses the new PK index.
  - INSERT ... ON CONFLICT (ticker) DO UPDATE still works (PG resolves
    ON CONFLICT against any UNIQUE constraint, not just PK).
  - INSERT ... ON CONFLICT (id) DO UPDATE works too if any handler uses it.

The 14 existing FKs continue to reference `id` — PG binds FK to columns,
not to a specific named UNIQUE/PK constraint, so swapping which
constraint provides the unique-index for id is transparent to them.

Revision ID: 20260524_1400
Revises: 20260524_1300
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_1400"
down_revision: str | None = "20260524_1300"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # 1. Explicit NOT NULL on ticker (defends against the implicit-NOT-NULL
    # loss when the (ticker)-PK constraint is dropped in step 2).
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ALTER COLUMN ticker SET NOT NULL"
    )

    # 2. Drop the old PK on (ticker). Safe because v2.2 P9 already cleared
    # the last FK that referenced ticker_classifications(ticker).
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT ticker_classifications_pkey"
    )

    # 3. Promote id to PRIMARY KEY. id is already NOT NULL + has the right
    # TKR-14 regex CHECK — PG accepts immediately. The existing
    # ticker_classifications_id_uniq stays in place because the 14 FKs are
    # bound to its index (PostgreSQL refuses DROP without CASCADE).
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD CONSTRAINT ticker_classifications_pkey PRIMARY KEY (id)"
    )

    # 4. Preserve uniqueness on ticker so ON CONFLICT (ticker) UPSERT paths
    # (parent_resolver_backfill, classify_tickers, _tkr14_backfill_fmp_profile)
    # continue to work.
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD CONSTRAINT ticker_classifications_ticker_key UNIQUE (ticker)"
    )


def downgrade() -> None:
    # Reverse the swap: ticker becomes PK again. id stays UNIQUE via the
    # untouched ticker_classifications_id_uniq constraint.
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT ticker_classifications_ticker_key"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT ticker_classifications_pkey"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD CONSTRAINT ticker_classifications_pkey PRIMARY KEY (ticker)"
    )
