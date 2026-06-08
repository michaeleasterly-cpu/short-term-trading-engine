"""ticker_history + ticker_lifecycle_events FK + ticker-scoped GIST exclusion.

Spec: ``docs/superpowers/specs/2026-06-08-data-foundation-systemic-fix-
design.md`` §3.3 / RC-2. Plan:
``docs/superpowers/plans/2026-06-08-data-foundation-reingest-plan.md`` Phase B
(20260608_0300 in the plan; renumbered 0200 here per the operator's two-
migration cut sequence — 0100 = resolver+triggers, 0200 = FK+exclusion).

This migration is applied AFTER the destructive spine cut (children wiped,
clean staged spine swapped into ``platform.ticker_classifications`` +
``ticker_history`` + the rebuilt satellites). On the DIRTY pre-cut spine these
constraints would FAIL (cross-entity same-ticker overlaps from the
FB/META/FISV reuse + windowless rows). On the CLEAN swapped spine they SUCCEED
(the staging gate proved P1/P2/P5 = 0 violators).

WHAT THIS ADDS:

  1. FK ``ticker_history.classification_id -> ticker_classifications.id``
     (ON UPDATE CASCADE ON DELETE RESTRICT — the project default for an
     identity-spine FK: ticker renames propagate, deletes are blocked to
     protect the spine). RC-2 named the MISSING FK as the cause behind most
     NULL classification_ids — re-anchoring a trigger cannot help when the
     window row is orphaned. This closes it.

  2. FK ``ticker_lifecycle_events.classification_id ->
     ticker_classifications.id`` (same semantics). The table is empty post-cut
     (0 rows), so the constraint adds with no validation cost.

  3. NEW ticker-scoped GIST exclusion on ``ticker_history``:
     ``EXCLUDE (ticker WITH =, daterange(valid_from,
     coalesce(valid_to,'infinity'),'[)') WITH &&)``. The EXISTING
     ``ticker_history_no_overlap`` exclusion is classification_id-scoped and
     does NOT prevent two DIFFERENT entities (classifications) holding
     overlapping windows under the SAME ticker string — the exact FB->Meta /
     SBNY / FISV reuse contamination. This ticker-scoped exclusion is the
     structural guarantee that a reused symbol's windows are disjoint across
     entities (half-open ``[)`` treats a contiguous handoff
     ``valid_to == next valid_from`` as NON-overlap, so a clean reuse handoff
     is permitted; a true overlap is rejected).

AUDIT (run pre-apply on the post-cut clean spine; expected 0 for all — if any
is > 0 the cut did NOT land clean, STOP and do not apply):
  * orphan ticker_history rows (classification_id not in ticker_classifications): 0
  * orphan ticker_lifecycle_events rows: 0 (table empty)
  * cross-entity same-ticker window overlaps (the new exclusion's violators): 0

DOWNGRADE drops the two FKs + the ticker-scoped exclusion (leaves the
pre-existing classification_id-scoped ``ticker_history_no_overlap`` untouched —
it was not created here).

Revision ID: 20260608_0200
Revises: 20260608_0150
Create Date: 2026-06-08
"""
from __future__ import annotations

from alembic import op

revision = "20260608_0200"
down_revision = "20260608_0150"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. FK ticker_history.classification_id -> ticker_classifications.id
    op.execute(
        """
        ALTER TABLE platform.ticker_history
        ADD CONSTRAINT ticker_history_classification_id_fk
        FOREIGN KEY (classification_id)
        REFERENCES platform.ticker_classifications(id)
        ON UPDATE CASCADE ON DELETE RESTRICT
        """
    )

    # 2. FK ticker_lifecycle_events.classification_id -> ticker_classifications.id
    op.execute(
        """
        ALTER TABLE platform.ticker_lifecycle_events
        ADD CONSTRAINT ticker_lifecycle_events_classification_id_fk
        FOREIGN KEY (classification_id)
        REFERENCES platform.ticker_classifications(id)
        ON UPDATE CASCADE ON DELETE RESTRICT
        """
    )

    # 3. NEW ticker-scoped GIST exclusion (cross-entity reuse disjointness).
    #    btree_gist must be present for the equality operator class on text;
    #    it is the same extension the existing classification_id exclusion uses.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute(
        """
        ALTER TABLE platform.ticker_history
        ADD CONSTRAINT ticker_history_ticker_no_overlap
        EXCLUDE USING gist (
            ticker WITH =,
            daterange(valid_from, COALESCE(valid_to, 'infinity'::date), '[)') WITH &&
        )
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE platform.ticker_history "
        "DROP CONSTRAINT IF EXISTS ticker_history_ticker_no_overlap"
    )
    op.execute(
        "ALTER TABLE platform.ticker_lifecycle_events "
        "DROP CONSTRAINT IF EXISTS ticker_lifecycle_events_classification_id_fk"
    )
    op.execute(
        "ALTER TABLE platform.ticker_history "
        "DROP CONSTRAINT IF EXISTS ticker_history_classification_id_fk"
    )
