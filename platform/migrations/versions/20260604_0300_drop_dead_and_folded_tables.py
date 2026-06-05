"""Plan 2 cutover — drop the dead / Tradier / re-derivable-folded tables.

Spec §2.2/§2.3. tradier_options_chains (Tradier closed), options_max_pain (no
producer) + its classification_id trigger + backing function, the empty
evidence/parity/forensics sidecars (folded into data_quality_log via ``kind``
in 0500), and ingestion_metrics (routes to ingest_manifest). split_pre_image_log,
ingest_quarantine, failed_alpha_ledger, AND ticker_lifecycle_events are KEPT here:
ticker_lifecycle_events still has a live producer (scripts/ops.py SEC-lifecycle
stage) — its fold into corporate_events is a Plan 3 re-ingest task (re-derive
Form 25/15 events into the M&A graph), not a Plan 2 schema drop; it is TRUNCATEd
with the ticker graph (it FKs ticker_classifications). macro_data + the
PRESERVE-class ops tables are untouched.

Pre-flight audit (live introspection 2026-06-04, alembic head 20260604_0200):
  * NO table FKs any DROP-set table (pg_constraint contype='f' scan → empty).
  * options_max_pain trigger is ``tg_options_max_pain_classification_id`` backed
    by ``platform.tg_set_classification_id_options_max_pain()`` (real names below).

Forward-only: ``downgrade`` raises NotImplementedError. The Plan 2 rollback path
is the Task-1 PRESERVE snapshot + Supabase PITR, not a re-create here.
"""
from __future__ import annotations

from alembic import op

revision = "20260604_0300"
down_revision = "20260604_0200"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # options_max_pain carries a classification_id BEFORE-INSERT trigger backed by
    # platform.tg_set_classification_id_options_max_pain() — drop trigger then function
    # before the table (CASCADE on the table would also clear the trigger, but the
    # backing function is schema-level and must be dropped explicitly).
    op.execute(
        "DROP TRIGGER IF EXISTS tg_options_max_pain_classification_id "
        "ON platform.options_max_pain"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS platform.tg_set_classification_id_options_max_pain() CASCADE"
    )
    # Explicit per-table DROPs (auditability for a destructive migration). KEEPS
    # split_pre_image_log, ingest_quarantine, failed_alpha_ledger, ingest_manifest.
    op.execute("DROP TABLE IF EXISTS platform.tradier_options_chains CASCADE")
    op.execute("DROP TABLE IF EXISTS platform.options_max_pain CASCADE")
    op.execute("DROP TABLE IF EXISTS platform.fundamentals_period_source_evidence CASCADE")
    op.execute("DROP TABLE IF EXISTS platform.parity_drift_log CASCADE")
    op.execute("DROP TABLE IF EXISTS platform.forensics_triggers CASCADE")
    op.execute("DROP TABLE IF EXISTS platform.ingestion_metrics CASCADE")


def downgrade() -> None:
    # Irreversible for data; the tables are recreated only by replaying the
    # ORIGINAL migrations that created them (not re-implemented here). The Plan 2
    # rollback path is the Task-1 snapshot + Supabase PITR, not this downgrade.
    raise NotImplementedError(
        "Plan 2 DROP migration is forward-only; roll back via the "
        "phase-1 snapshot + Supabase PITR."
    )
