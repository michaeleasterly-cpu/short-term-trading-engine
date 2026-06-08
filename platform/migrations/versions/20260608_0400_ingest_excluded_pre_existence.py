"""Durable pre-existence exclusion evidence trail (Phase C cleanup).

Spec: ``docs/superpowers/specs/2026-06-08-data-foundation-systemic-fix-
design.md`` §0 / §5 (the would-reject adjudication). Plan:
``docs/superpowers/plans/2026-06-08-data-foundation-reingest-plan.md`` Phase C.

WHAT THIS DOES (additive; one new table, no behavior change to existing paths):

Creates ``platform.ingest_excluded_pre_existence`` — the PERMANENT evidence
trail for child-table rows that were adjudicated as pre-existence and DELETED
from (or never inserted into) their child table during the Phase-C re-ingest
cleanup.

The Phase-C re-ingest loaded the 6 child tables in identity-contract LOG mode;
6,777 (table, ticker, event_date) writes could not be window-resolved and were
recorded in ``platform.identity_contract_log`` (the TRANSIENT diagnostic trail,
zeroed by the Phase-C3 gate ``count(*) == 0``). Each was then adjudicated
against authoritative SEC evidence (the entity's CIK earliest filing date,
cross-checked against the bulk ``submissions.zip`` — DB ``first_public_filing_
date`` matched the SEC bulk earliest for all 1,107 non-null-CIK entities, 0
mismatches):

  * WIDEN (1 row — SVA 2025-07-08): the SAME entity (CIK 0001084201, still an
    active SEC filer through 2026) had a corporate action AFTER a wrongly-closed
    ``lifetime_end``. Resolved by widening the window (lifetime_end -> NULL) +
    re-resolving — NOT recorded here.
  * EXCLUDE (6,776 rows): the event predates the entity's OWN earliest SEC
    filing (predecessor entity reusing the ticker — e.g. FOX 2017-18 = 21st
    Century Fox under a different CIK) OR there is no SEC entity at all (FMP
    pre-IPO/ADR/ETF synthetic artifact — PBR.A, GPT, LQ, NATO). These are NOT
    the current entity's data; attributing them would be cross-entity
    contamination. 43 of the original 6,776 fundamentals were RESCUED by the
    ``filing_date`` fallback (first post-IPO 10-Q whose fiscal period predates
    the IPO but whose FILING happened in-window) and are NOT excluded; the net
    excluded set is 6,733.

This table is the durable, evidence-backed record of WHY each row was removed —
the anti-fake-green proof that no resolvable row was deleted and every excluded
row is genuine pre-existence.

## Schema rationale (controls-audit §13 #11)

Readers (named code paths that will query the new table):
  - Phase-C audit + this PR's report (the widen/exclude adjudication proof).
  - ops/dashboard pre-existence-exclusion monitoring (a future re-ingest that
    re-excludes the same (table, ticker, event_date) should match this trail —
    the loader patch in ``data/rebuild_2026-06-04/rebuild_child_tables_load.py``
    consults the same SEC-earliest rule at source).

Writers (canonical writer; single-writer):
  - The Phase-C cleanup script (operator-local, this PR's apply step) and any
    future re-ingest loader's pre-existence skip path. Single-writer in
    spirit — only the identity-first child loader writes it.

Existing-table alternative considered:
  - ``identity_contract_log``: rejected as the PERMANENT home. The contract log
    is the TRANSIENT would-reject diagnostic trail, defined (migration
    20260608_0100) to be zeroed by the Phase-C3 gate (``count(*) == 0`` proves
    a clean re-ingest). Storing permanent exclusion-evidence there would make
    the C3 gate un-satisfiable. The two have opposite lifecycles: the log is
    cleared once adjudicated; this trail is kept forever as the deletion
    audit record. Distinct purpose => distinct table.
  - ``data_quality_log``: rejected — validation-layer per-check detector
    substrate, not a write-time row-level exclusion record.

Why not extend the existing identity / lifecycle substrate?
  - ``ticker_history`` / ``ticker_classifications`` model the ENTITY spine
    (which entity owned a ticker when). They cannot record "this child row was
    attributed to the wrong entity and removed" — that is row-level provenance
    of a DELETE, which no identity-spine table represents. The 15 SCD-2
    triggers resolve identity FORWARD on insert; they have nowhere to record a
    backward adjudication of an already-excluded row.

Revision ID: 20260608_0400
Revises: 20260608_0200
Create Date: 2026-06-08
"""
from __future__ import annotations

from alembic import op

revision = "20260608_0400"
down_revision = "20260608_0200"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.ingest_excluded_pre_existence (
            id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tbl                 text NOT NULL,
            ticker              text NOT NULL,
            event_date          date NOT NULL,
            resolved_cik        text,
            resolved_cid        text,
            entity_lifetime_start date,
            sec_earliest_filing date,
            reason              text NOT NULL,
            excluded_at         timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ingest_excluded_pre_existence_reason_ck
                CHECK (reason IN (
                    'pre_existence_predecessor',
                    'pre_existence_artifact_no_sec_entity'
                ))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ingest_excluded_pre_existence_tbl_ticker_idx "
        "ON platform.ingest_excluded_pre_existence (tbl, ticker, event_date)"
    )


def downgrade() -> None:
    op.execute(
        "DROP TABLE IF EXISTS platform.ingest_excluded_pre_existence"
    )
