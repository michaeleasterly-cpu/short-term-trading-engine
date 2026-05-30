"""issuer lifecycle evidence foundation — SEC Form 25 / 15 evidence model

Revision ID: 20260530_0300
Revises: 20260530_0200
Create Date: 2026-05-30

P2a foundation for evidence-based delisting state tracking. Adds
nullable evidence columns to ``platform.ticker_classifications`` PLUS
a sibling append-only event log ``platform.ticker_lifecycle_events``
that preserves the audit trail (Form 25 then Form 15 is a real two-
step lifecycle; the projection columns alone would lose Form 25
evidence when Form 15 arrives).

ADDS NULLABLE COLUMNS + NEW TABLE — no validator semantics change in
this migration. The new fields are foundation for the future P2b
lifecycle-bound validation wiring; this migration does not touch any
validator code, the FinalLaneVerdict, or the capital gate.

Naming: ``issuer_lifecycle_*`` prefix avoids the documented collision
with ``EngineProfile.lifecycle_state`` (engine SDLC) — expert review
flagged this as a non-negotiable bug. Engine lifecycle = LAB/PAPER/
LIVE/RETIRED; issuer lifecycle = active/delist_pending/delist_effective/
deregistered. Different domains, prefix keeps grep audits clean.

Columns added to ``platform.ticker_classifications``:

  issuer_lifecycle_state          text   -- 'active' | 'delist_pending' |
                                            'delist_effective' | 'deregistered'
                                            (CHECK enum)
  issuer_lifecycle_state_source   text   -- 'sec_form_25' | 'sec_form_15' |
                                            'sec_form_8k' | 'fmp_profile' |
                                            'alpaca_asset_status' | 'manual'
                                            (CHECK enum). Precedence
                                            (highest wins): manual >
                                            sec_form_15 > sec_form_25 >
                                            sec_form_8k > alpaca > fmp.
  issuer_lifecycle_event_date     date   -- report_date if present else
                                            filing_date of the latest
                                            evidence event
  issuer_lifecycle_evidence_url   text   -- canonical SEC Archives URL
                                            (built from accessionNumber):
                                            ``https://www.sec.gov/Archives
                                            /edgar/data/<cik_int>/<acc_no_
                                            dashes>/<acc_with_dashes>-
                                            index.htm``
  issuer_lifecycle_updated_at     timestamptz -- when backfill last ran

New table ``platform.ticker_lifecycle_events`` (append-only audit log):

  id                bigserial PRIMARY KEY
  classification_id text NOT NULL  -- FK → ticker_classifications.id (TKR-14)
  ticker            text NOT NULL  -- ticker at event time (denormalized
                                      for diagnostics)
  form_type         text NOT NULL  -- '25' | '25-NSE' | '15' | '15-12G' |
                                      '15-12B' | '15F' | '15-15D' (SEC
                                      verbatim — preserves issuer-vs-
                                      exchange distinction for forensics)
  filing_date       date           -- date SEC stamped the filing
  report_date       date           -- date the issuer claimed as effective
  accession_number  text           -- SEC accession (e.g. '0000320193-25-000123')
  source            text NOT NULL  -- 'sec_form_25' | 'sec_form_15' |
                                      'sec_form_8k' | 'fmp_profile' |
                                      'alpaca_asset_status' | 'manual'
  evidence_url      text           -- canonical SEC Archives URL or NULL
  ingested_at       timestamptz NOT NULL DEFAULT NOW()

  UNIQUE (classification_id, form_type, accession_number)
  INDEX  (classification_id, filing_date DESC)

The UNIQUE constraint makes the backfill UPSERT idempotent: re-running
the SEC pull cannot duplicate events. The append-only design means the
Form 25 row stays in the log even after Form 15 arrives (and the
projection on ticker_classifications flips to 'deregistered').

Constraints + indexes:
  * issuer_lifecycle_state CHECK enum (4 states + NULL).
  * issuer_lifecycle_state_source CHECK enum (6 sources + NULL).
  * ticker_lifecycle_events.form_type CHECK enum (7 form types).
  * ticker_lifecycle_events.source CHECK enum (6 sources).
  * Partial index on (issuer_lifecycle_state, country) for the
    future issuer-class router predicate.

Idempotency: every ADD COLUMN uses IF NOT EXISTS; the new table uses
CREATE TABLE IF NOT EXISTS; every CHECK constraint is DROP-then-ADD
guarded.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260530_0300"
down_revision: str | None = "20260530_0200"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_VALID_STATES = (
    "active", "delist_pending", "delist_effective", "deregistered",
)
_VALID_SOURCES = (
    "sec_form_25", "sec_form_15", "sec_form_8k",
    "fmp_profile", "alpaca_asset_status", "manual",
)
_VALID_FORM_TYPES = (
    # SEC Form 25 (delist notice) — verbatim variants.
    "25", "25-NSE",
    # SEC Form 15 (deregistration) — verbatim variants.
    "15", "15-12G", "15-12B", "15F", "15-15D",
)


def upgrade() -> None:
    # 1. Projection columns on ticker_classifications. Nullable, no defaults.
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD COLUMN IF NOT EXISTS issuer_lifecycle_state text"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD COLUMN IF NOT EXISTS issuer_lifecycle_state_source text"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD COLUMN IF NOT EXISTS issuer_lifecycle_event_date date"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD COLUMN IF NOT EXISTS issuer_lifecycle_evidence_url text"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD COLUMN IF NOT EXISTS issuer_lifecycle_updated_at timestamptz"
    )

    # 2. CHECK constraints — guarded so re-runs are safe.
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT IF EXISTS "
        "ticker_classifications_issuer_lifecycle_state_chk"
    )
    valid_states = ", ".join(f"'{v}'::text" for v in _VALID_STATES)
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD CONSTRAINT ticker_classifications_issuer_lifecycle_state_chk "
        f"CHECK (issuer_lifecycle_state IS NULL OR "
        f"issuer_lifecycle_state = ANY(ARRAY[{valid_states}]))"
    )

    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT IF EXISTS "
        "ticker_classifications_issuer_lifecycle_state_source_chk"
    )
    valid_sources = ", ".join(f"'{v}'::text" for v in _VALID_SOURCES)
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD CONSTRAINT ticker_classifications_issuer_lifecycle_state_source_chk "
        f"CHECK (issuer_lifecycle_state_source IS NULL OR "
        f"issuer_lifecycle_state_source = ANY(ARRAY[{valid_sources}]))"
    )

    # 3. Partial index for the future issuer-class router predicate.
    op.execute(
        "CREATE INDEX IF NOT EXISTS "
        "ix_ticker_classifications_issuer_lifecycle "
        "ON platform.ticker_classifications "
        "(issuer_lifecycle_state, country) "
        "WHERE issuer_lifecycle_state IS NOT NULL"
    )

    # 4. Append-only event log table.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.ticker_lifecycle_events (
            id                bigserial PRIMARY KEY,
            classification_id text NOT NULL,
            ticker            text NOT NULL,
            form_type         text NOT NULL,
            filing_date       date,
            report_date       date,
            accession_number  text,
            source            text NOT NULL,
            evidence_url      text,
            ingested_at       timestamptz NOT NULL DEFAULT NOW()
        )
        """
    )

    # 5. CHECK constraints on the event log — guarded.
    op.execute(
        "ALTER TABLE platform.ticker_lifecycle_events "
        "DROP CONSTRAINT IF EXISTS ticker_lifecycle_events_form_type_chk"
    )
    valid_forms = ", ".join(f"'{v}'::text" for v in _VALID_FORM_TYPES)
    op.execute(
        "ALTER TABLE platform.ticker_lifecycle_events "
        "ADD CONSTRAINT ticker_lifecycle_events_form_type_chk "
        f"CHECK (form_type = ANY(ARRAY[{valid_forms}]))"
    )

    op.execute(
        "ALTER TABLE platform.ticker_lifecycle_events "
        "DROP CONSTRAINT IF EXISTS ticker_lifecycle_events_source_chk"
    )
    op.execute(
        "ALTER TABLE platform.ticker_lifecycle_events "
        "ADD CONSTRAINT ticker_lifecycle_events_source_chk "
        f"CHECK (source = ANY(ARRAY[{valid_sources}]))"
    )

    # 6. UNIQUE on (classification_id, form_type, accession_number) for
    # idempotent ON CONFLICT DO NOTHING upserts. Cannot UNIQUE on a
    # NULLABLE accession_number column directly with strict equality;
    # Postgres treats NULLs as distinct. Use a partial unique index that
    # only enforces uniqueness when accession_number is non-NULL (the
    # SEC-sourced rows always carry one; manual entries may not).
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "ux_ticker_lifecycle_events_cid_form_acc "
        "ON platform.ticker_lifecycle_events "
        "(classification_id, form_type, accession_number) "
        "WHERE accession_number IS NOT NULL"
    )

    # 7. Lookup index — newest events first per classification.
    op.execute(
        "CREATE INDEX IF NOT EXISTS "
        "ix_ticker_lifecycle_events_cid_filing "
        "ON platform.ticker_lifecycle_events "
        "(classification_id, filing_date DESC NULLS LAST)"
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS "
        "platform.ix_ticker_lifecycle_events_cid_filing"
    )
    op.execute(
        "DROP INDEX IF EXISTS "
        "platform.ux_ticker_lifecycle_events_cid_form_acc"
    )
    op.execute(
        "ALTER TABLE platform.ticker_lifecycle_events "
        "DROP CONSTRAINT IF EXISTS ticker_lifecycle_events_source_chk"
    )
    op.execute(
        "ALTER TABLE platform.ticker_lifecycle_events "
        "DROP CONSTRAINT IF EXISTS ticker_lifecycle_events_form_type_chk"
    )
    op.execute("DROP TABLE IF EXISTS platform.ticker_lifecycle_events")
    op.execute(
        "DROP INDEX IF EXISTS "
        "platform.ix_ticker_classifications_issuer_lifecycle"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT IF EXISTS "
        "ticker_classifications_issuer_lifecycle_state_source_chk"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT IF EXISTS "
        "ticker_classifications_issuer_lifecycle_state_chk"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP COLUMN IF EXISTS issuer_lifecycle_updated_at"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP COLUMN IF EXISTS issuer_lifecycle_evidence_url"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP COLUMN IF EXISTS issuer_lifecycle_event_date"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP COLUMN IF EXISTS issuer_lifecycle_state_source"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP COLUMN IF EXISTS issuer_lifecycle_state"
    )
