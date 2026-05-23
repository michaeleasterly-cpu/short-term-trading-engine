"""Phase 2 — NOT-VALID-FIRST bulk FK add for ALL 14 in-scope child tables.

Per v2 plan §4 and v2 spec §5.1. The v2 inversion: ship every FK as
`ADD CONSTRAINT ... NOT VALID` in ONE migration. Fast lock (no row
scan). From the moment this migration commits, producers cannot
create new orphans — `NOT VALID` enforces on INSERT/UPDATE
immediately; only EXISTING rows remain unvalidated until per-table
VALIDATE in Phase 4.

In-scope: 14 tables (NOT 15 — per Phase 0 finding `insider_mspr_daily`
is a VIEW, structurally cannot have FKs).

Pre-flight gates (per v2 plan §4.2):
- Phase 0 deliverables green (universe_candidates(ticker) index added).
- Phase 1 migrations landed (rename + country backfilled).
- Live alembic head = 20260523_0601 (Phase 1.2).

FK default: `ON UPDATE CASCADE ON DELETE RESTRICT`. Per
.claude/agents/db-architect.md §4: never CASCADE on ticker FK
(protect data; force producer to handle deletion explicitly).

Two tables use `symbol` instead of `ticker`:
- options_max_pain (PK: symbol, expiration_date, observed_date)
- insider_sentiment (PK: symbol, year, month)

Revision ID: 20260523_0700
Revises: 20260523_0601
Create Date: 2026-05-23
"""
from alembic import op

revision: str = "20260523_0700"
down_revision: str | None = "20260523_0601"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


# (table_name, fk_column_name) — explicit list so Phase 4 cleanup can
# iterate in the same order. Order is "ascending orphan count" per
# Phase 0 audit baseline (smallest first; prices_daily LAST).
_FKS: tuple[tuple[str, str], ...] = (
    # Zero-orphan tables (immediate VALIDATE-ready)
    ("insider_transactions", "ticker"),
    ("sec_material_events", "ticker"),
    ("borrow_rates", "ticker"),
    ("social_sentiment", "ticker"),
    ("options_max_pain", "symbol"),
    ("insider_sentiment", "symbol"),
    # Small-orphan tables (ascending)
    ("universe_candidates", "ticker"),  # 1 orphan
    ("short_interest", "ticker"),       # 3 orphans
    ("liquidity_tiers", "ticker"),      # 8 orphans
    ("earnings_events", "ticker"),      # 12 orphans
    ("spread_observations", "ticker"),  # 33 orphans
    ("fundamentals_quarterly", "ticker"),  # 135 orphans
    ("corporate_actions", "ticker"),    # 1,506 orphans
    # Last: the 21M-row beast with 335,159 orphans
    ("prices_daily", "ticker"),
)


def _constraint_name(table: str, col: str) -> str:
    return f"fk_{table}_{col}"


def upgrade() -> None:
    # Per v2 spec §9.1: SET LOCAL statement_timeout. Cluster default is
    # 120s (verified Phase 0); 5min covers 14 sub-second ALTER ops.
    # If role-level cap rejects this, the operator raises via Supabase
    # dashboard.
    op.execute("SET LOCAL statement_timeout = '5min'")

    for table, col in _FKS:
        constraint = _constraint_name(table, col)
        op.execute(f"""
            ALTER TABLE platform.{table}
                ADD CONSTRAINT {constraint}
                FOREIGN KEY ({col})
                REFERENCES platform.ticker_classifications(ticker)
                ON UPDATE CASCADE ON DELETE RESTRICT
                NOT VALID
        """)


def downgrade() -> None:
    # Pure DDL DROP per constraint — fast, no row scan.
    for table, col in reversed(_FKS):
        constraint = _constraint_name(table, col)
        op.execute(f"""
            ALTER TABLE platform.{table}
                DROP CONSTRAINT IF EXISTS {constraint}
        """)
