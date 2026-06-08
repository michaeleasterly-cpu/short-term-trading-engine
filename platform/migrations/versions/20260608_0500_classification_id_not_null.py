"""SET NOT NULL on classification_id for the 7 identity-bearing child tables.

Spec: ``docs/superpowers/specs/2026-06-08-data-foundation-systemic-fix-
design.md`` §0 / §5 (the data-foundation finalize). Plan:
``docs/superpowers/plans/2026-06-08-data-foundation-reingest-plan.md`` Phase D.

This is the FINAL lock of the data-foundation re-ingest: it flips
``classification_id`` from NULLABLE to NOT NULL on the 7 child tables, making
"every ticker-bearing row resolves to an entity window" a structural invariant
the database enforces (not merely a validator that can be skipped). After this,
a write that cannot window-resolve cannot land NULL — it either resolves (the
15 SCD-2 BEFORE-INSERT triggers + the unified resolver) or, in hard mode, the
INSERT is rejected.

PRE-APPLY AUDIT (run against live BEFORE this migration; ALL must be 0 — if any
is > 0, STOP, the re-ingest did not land clean and SET NOT NULL would fail):

    SELECT count(*) FROM platform.<tbl> WHERE classification_id IS NULL;

  prices_daily            0
  corporate_actions       0
  earnings_events         0
  fundamentals_quarterly  0   (5,331 pre-existence rows DELETED + evidenced in
                               platform.ingest_excluded_pre_existence; 43 first-
                               post-IPO filings RESCUED via the filing_date
                               fallback)
  sec_periodic_filings    0   (empty)
  aar_events              0   (empty)
  short_interest          0

The contract-log adjudication (6,777 would-rejects) resolved to 1 WIDEN (SVA
lifetime_end -> NULL, entity still SEC-filing) + 43 filing_date-rescued
fundamentals + 6,733 evidenced pre-existence exclusions (recorded in
``platform.ingest_excluded_pre_existence``, migration 20260608_0400). No
resolvable row was deleted; every excluded row is genuine pre-existence
(predecessor entity reusing the ticker, or an FMP synthetic artifact with no
SEC entity). insider_transactions + sec_material_events were ALREADY NOT NULL
(their nullable-soften never inserted the unresolvable rows) — they are not
re-altered here.

DOWNGRADE drops NOT NULL back to nullable on all 7 (DROP NOT NULL is always
safe — it widens the domain).

Revision ID: 20260608_0500
Revises: 20260608_0400
Create Date: 2026-06-08
"""
from __future__ import annotations

from alembic import op

revision = "20260608_0500"
down_revision = "20260608_0400"
branch_labels = None
depends_on = None

# The 7 formerly-nullable classification_id columns (insider_transactions +
# sec_material_events were already NOT NULL and are intentionally excluded).
_TABLES: tuple[str, ...] = (
    "prices_daily",
    "corporate_actions",
    "earnings_events",
    "fundamentals_quarterly",
    "sec_periodic_filings",
    "aar_events",
    "short_interest",
)


def upgrade() -> None:
    for tbl in _TABLES:
        op.execute(
            f"ALTER TABLE platform.{tbl} "
            f"ALTER COLUMN classification_id SET NOT NULL"
        )


def downgrade() -> None:
    for tbl in _TABLES:
        op.execute(
            f"ALTER TABLE platform.{tbl} "
            f"ALTER COLUMN classification_id DROP NOT NULL"
        )
