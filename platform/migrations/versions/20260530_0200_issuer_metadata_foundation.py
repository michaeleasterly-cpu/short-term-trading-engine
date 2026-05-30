"""issuer metadata foundation — SEC DEI evidence columns

Revision ID: 20260530_0200
Revises: 20260530_0100
Create Date: 2026-05-30

P0 foundation for the evidence-based fundamentals-validation rewrite
(spec docs/superpowers/specs/2026-05-30-asset-class-refinement.md
follow-up; expert audit verdict REVISE_ARCHITECTURE).

ADDS NULLABLE COLUMNS ONLY — no validator semantics change in this
migration. The new fields are foundation for the future five-state
fundamentals_quarterly_completeness rewrite which will read from
them; this migration does not touch any validator code, the
FinalLaneVerdict, or the capital gate.

Columns added to ``platform.ticker_classifications``:

  sec_document_type_primary    text    -- 10-Q / 10-K / 20-F / 40-F / 6-K
                                          (the dispositive issuer-class
                                          signal — observed from SEC EDGAR
                                          dei:DocumentType histogram, NOT
                                          taxonomy-derived from country)
  sec_document_type_history    jsonb   -- {"10-Q": 42, "10-K": 11, ...}
                                          full histogram for diagnostics
  first_public_filing_date     date    -- min(dei:DocumentPeriodEndDate)
                                          for the primary DocumentType
                                          (SEC-derived ONLY — not FMP
                                          ipoDate, which conflates SPAC
                                          predecessor history)
  fiscal_year_end_month        smallint-- dei:CurrentFiscalYearEndDate
                                          month component (1-12).
                                          AZO=8, BNED=1, default=12 etc.
  last_filing_date             date    -- max(dei:DocumentPeriodEndDate)
                                          OR SEC submissions.json filings
                                          .recent.filingDate max — used
                                          downstream to corroborate
                                          delisting via filing cessation
  metadata_source              text    -- which adapter produced the
                                          metadata: 'sec_companyfacts'
                                          | 'sec_submissions' | 'manual'
  metadata_updated_at          timestamptz -- when the backfill stage
                                          last ran for this row
  cik_source                   text    -- 'sec_ticker_map' | 'fmp' |
                                          'manual' — provenance for CIK
                                          backfill audit

Constraints:
  * fiscal_year_end_month CHECK between 1 and 12 OR NULL.
  * metadata_source CHECK in {'sec_companyfacts', 'sec_submissions',
    'manual', 'fmp_profile', NULL}.
  * cik_source CHECK in {'sec_ticker_map', 'fmp', 'manual', NULL}.

Index:
  * (sec_document_type_primary, country) — speeds up the future
    issuer-class router predicate.

Idempotency: every ADD COLUMN uses IF NOT EXISTS.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260530_0200"
down_revision: str | None = "20260530_0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_VALID_METADATA_SOURCES = (
    "sec_companyfacts", "sec_submissions",
    "manual", "fmp_profile",
)
_VALID_CIK_SOURCES = (
    "sec_ticker_map", "fmp", "manual",
)


def upgrade() -> None:
    # 1. New evidence columns. Nullable; no defaults.
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD COLUMN IF NOT EXISTS sec_document_type_primary text"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD COLUMN IF NOT EXISTS sec_document_type_history jsonb"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD COLUMN IF NOT EXISTS first_public_filing_date date"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD COLUMN IF NOT EXISTS fiscal_year_end_month smallint"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD COLUMN IF NOT EXISTS last_filing_date date"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD COLUMN IF NOT EXISTS metadata_source text"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD COLUMN IF NOT EXISTS metadata_updated_at timestamptz"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD COLUMN IF NOT EXISTS cik_source text"
    )

    # 2. CHECK constraints — each guarded so re-runs are safe.
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT IF EXISTS "
        "ticker_classifications_fiscal_year_end_month_chk"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD CONSTRAINT ticker_classifications_fiscal_year_end_month_chk "
        "CHECK (fiscal_year_end_month IS NULL OR "
        "       (fiscal_year_end_month BETWEEN 1 AND 12))"
    )

    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT IF EXISTS "
        "ticker_classifications_metadata_source_chk"
    )
    valid_meta = ", ".join(f"'{v}'::text" for v in _VALID_METADATA_SOURCES)
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD CONSTRAINT ticker_classifications_metadata_source_chk "
        f"CHECK (metadata_source IS NULL OR "
        f"metadata_source = ANY(ARRAY[{valid_meta}]))"
    )

    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT IF EXISTS "
        "ticker_classifications_cik_source_chk"
    )
    valid_cik = ", ".join(f"'{v}'::text" for v in _VALID_CIK_SOURCES)
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD CONSTRAINT ticker_classifications_cik_source_chk "
        f"CHECK (cik_source IS NULL OR "
        f"cik_source = ANY(ARRAY[{valid_cik}]))"
    )

    # 3. Index for the future issuer-class router predicate.
    op.execute(
        "CREATE INDEX IF NOT EXISTS "
        "ix_ticker_classifications_sec_doctype "
        "ON platform.ticker_classifications "
        "(sec_document_type_primary, country) "
        "WHERE sec_document_type_primary IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS "
        "platform.ix_ticker_classifications_sec_doctype"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT IF EXISTS "
        "ticker_classifications_cik_source_chk"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT IF EXISTS "
        "ticker_classifications_metadata_source_chk"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT IF EXISTS "
        "ticker_classifications_fiscal_year_end_month_chk"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP COLUMN IF EXISTS cik_source"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP COLUMN IF EXISTS metadata_updated_at"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP COLUMN IF EXISTS metadata_source"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP COLUMN IF EXISTS last_filing_date"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP COLUMN IF EXISTS fiscal_year_end_month"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP COLUMN IF EXISTS first_public_filing_date"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP COLUMN IF EXISTS sec_document_type_history"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP COLUMN IF EXISTS sec_document_type_primary"
    )
