"""Move btree_gist extension out of public schema (Supabase advisor 0014).

The v2.2 P2 migration `20260524_0100_create_ticker_history.py` installs
`btree_gist` via `CREATE EXTENSION IF NOT EXISTS btree_gist`. Postgres
defaults to the `public` schema; Supabase's database advisor (lint 0014)
flags this because objects in `public` are exposed by default through
the Supabase APIs (PostgREST). The fix is to move the extension to a
dedicated `extensions` schema.

Per Supabase advisor recommendation:

    CREATE SCHEMA IF NOT EXISTS extensions;
    ALTER EXTENSION btree_gist SET SCHEMA extensions;

The `EXCLUDE USING gist` constraint on `platform.ticker_history` uses
the schema-search-path resolution; once btree_gist lives in
`extensions`, the constraint still works because Supabase's default
search_path includes `extensions`. Verified locally.

Downgrade restores the extension to `public` for parity with the
original P2 state (rare; mostly here for replay-from-zero correctness).

Revision ID: 20260524_0200
Revises: 20260524_0100
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_0200"
down_revision: str | None = "20260524_0100"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS extensions")
    op.execute("ALTER EXTENSION btree_gist SET SCHEMA extensions")


def downgrade() -> None:
    op.execute("ALTER EXTENSION btree_gist SET SCHEMA public")
    # Note: deliberately do NOT drop the extensions schema — it may carry
    # other extensions installed independently of this migration.
