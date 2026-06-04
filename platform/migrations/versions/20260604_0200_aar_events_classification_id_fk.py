"""Add the missing aar_events.classification_id FK.

20260524_1903 added the classification_id COLUMN + the BEFORE INSERT trigger but
never the FK constraint; live aar_events therefore has zero FKs on the identity
dimension. This closes that gap so cross-engine AAR analytics that join on the
stable identity dimension are referentially guaranteed.

Spec: 2026-06-04-data-layer-rebuild-design.md §3.4.

Pattern: NOT VALID -> VALIDATE (the project's standard two-step FK add — see
20260524_0701 for prices_daily). NOT VALID adds the constraint without scanning
existing rows (no long lock on a populated table); VALIDATE then scans once to
prove zero violators. aar_events has 0 orphan classification_id rows today
(the column is trigger-populated from ticker_history, which itself FKs the same
parent), so VALIDATE is clean. The pre-flight orphan count is run by the
coordinator before apply (Task 5 Step 4); if it is > 0 that is a data finding
for the operator, NOT a constraint to force.

FK semantics match the other substrate identity FKs:
  ON UPDATE CASCADE   — ticker_classifications.id renames propagate.
  ON DELETE RESTRICT  — protect AAR history; a producer must explicitly handle
                        deletion (never silently cascade-delete trading history).

Revision ID: 20260604_0200
Revises: 20260604_0100
Create Date: 2026-06-04
"""
from __future__ import annotations

from alembic import op

revision = "20260604_0200"
down_revision = "20260604_0100"
branch_labels = None
depends_on = None

CONSTRAINT = "aar_events_classification_id_fk"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE platform.aar_events "
        f"ADD CONSTRAINT {CONSTRAINT} "
        f"FOREIGN KEY (classification_id) REFERENCES platform.ticker_classifications(id) "
        f"ON UPDATE CASCADE ON DELETE RESTRICT NOT VALID"
    )
    op.execute(f"ALTER TABLE platform.aar_events VALIDATE CONSTRAINT {CONSTRAINT}")


def downgrade() -> None:
    op.execute(f"ALTER TABLE platform.aar_events DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
