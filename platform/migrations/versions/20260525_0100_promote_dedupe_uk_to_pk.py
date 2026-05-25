"""Promote the dedupe UNIQUE NOT NULL constraints to PRIMARY KEY on
insider_transactions + sec_material_events.

Database acceptance audit (2026-05-25) flagged these as "tables
without PRIMARY KEY". The row-uniqueness invariant is already
enforced — both tables have UNIQUE NOT NULL on the natural key:

  insider_transactions_dedupe_uk:
    UNIQUE (ticker, filing_date, insider_name, transaction_type, shares)
  sec_material_events_dedupe_uk:
    UNIQUE (ticker, filing_date, event_type)

Audit verified 0 duplicates on both. Promoting UNIQUE → PRIMARY KEY
satisfies the acceptance criterion ("Primary keys exist where
required") without any data change. No outbound FK references either
table today.

Composite PK is intentional: these are terminal child tables (no
inbound FKs), the natural key is unambiguous, and adding a surrogate
BIGSERIAL id would mean another roundtrip on every insert without
benefit.

Revision ID: 20260525_0100
Revises: 20260525_0000
Create Date: 2026-05-25
"""
from alembic import op

revision: str = "20260525_0100"
down_revision: str | None = "20260525_0000"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # insider_transactions
    op.execute(
        "ALTER TABLE platform.insider_transactions "
        "DROP CONSTRAINT IF EXISTS insider_transactions_dedupe_uk"
    )
    op.execute(
        "ALTER TABLE platform.insider_transactions "
        "ADD CONSTRAINT insider_transactions_pk "
        "PRIMARY KEY (ticker, filing_date, insider_name, transaction_type, shares)"
    )
    # sec_material_events
    op.execute(
        "ALTER TABLE platform.sec_material_events "
        "DROP CONSTRAINT IF EXISTS sec_material_events_dedupe_uk"
    )
    op.execute(
        "ALTER TABLE platform.sec_material_events "
        "ADD CONSTRAINT sec_material_events_pk "
        "PRIMARY KEY (ticker, filing_date, event_type)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE platform.insider_transactions "
        "DROP CONSTRAINT IF EXISTS insider_transactions_pk"
    )
    op.execute(
        "ALTER TABLE platform.insider_transactions "
        "ADD CONSTRAINT insider_transactions_dedupe_uk "
        "UNIQUE (ticker, filing_date, insider_name, transaction_type, shares)"
    )
    op.execute(
        "ALTER TABLE platform.sec_material_events "
        "DROP CONSTRAINT IF EXISTS sec_material_events_pk"
    )
    op.execute(
        "ALTER TABLE platform.sec_material_events "
        "ADD CONSTRAINT sec_material_events_dedupe_uk "
        "UNIQUE (ticker, filing_date, event_type)"
    )
