"""Drop `platform.issuers_v` shadow view.

Engine-abstraction session (2026-05-24 handoff) flagged this as
defect #2. The view predates the real `platform.issuers` table
(added by the corp-history substrate in `20260524_1600_corp_history_v1`)
and now shadows it with contradictory data:

  - `platform.issuers_v` (13,840 rows): `SELECT DISTINCT cik AS
    issuer_id, ... FROM ticker_classifications WHERE cik IS NOT NULL`
    — view-derived from the classification dimension.
  - `platform.issuers` (29 rows): the canonical issuer table — real
    rows minted by the corporate_events_seed + corp_history_edgar_backfill
    stages with `issuer_id = 'CIK<padded>'` (NOT raw `cik`).

Different consumers reading the two will see contradictory data
(different row counts AND different issuer_id format). The real
table supersedes the view. Drop the view; downgrade recreates it
verbatim if some script we don't know about still depends on it.

Revision ID: 20260524_1902
Revises: 20260524_1901
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_1902"
down_revision: str | None = "20260524_1901"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS platform.issuers_v")


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE VIEW platform.issuers_v AS
        SELECT DISTINCT
            cik AS issuer_id,
            cik,
            current_legal_name AS legal_name,
            country AS country_of_incorp
        FROM platform.ticker_classifications
        WHERE cik IS NOT NULL
        """
    )
