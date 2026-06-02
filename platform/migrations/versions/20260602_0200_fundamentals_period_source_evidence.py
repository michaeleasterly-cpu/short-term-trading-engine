"""fundamentals_period_source_evidence — dual-source attempt substrate

Revision ID: 20260602_0200
Revises: 20260602_0100
Create Date: 2026-06-03

Per the `excluded_confirmed_data_gap` validator-semantics arc — spec PR
#450 (`docs/superpowers/specs/2026-06-02-excluded-confirmed-data-gap-validator-semantics.md`)
+ plan PR #451 (`docs/superpowers/plans/2026-06-02-excluded-confirmed-data-gap-validator-semantics-plan.md`).

This migration adds `platform.fundamentals_period_source_evidence` —
a single tightly-scoped substrate that records per-`(ticker,
period_end_date, source)` attempts from the FMP cascade AND the SEC
companyfacts fallback. The validator's `fundamentals_quarterly_completeness`
check joins this substrate to route dual-source-confirmed-empty
periods to the existing `excluded_confirmed_data_gap` bucket, BUT
only when:

  * BOTH legs (one `fmp_*` row + one `sec_companyfacts` row) are
    fresh (`attempted_at >= NOW() - INTERVAL '180 days'`),
  * BOTH legs are `outcome IN ('empty', 'extract_none')`,
  * NEITHER leg is `outcome='fetch_failure'` in the freshness window.

The new `excluded_confirmed_data_gap_evidenced` sub-counter logs
through structlog at completion; `CheckResult` stays frozen.

Schema:

  ticker            text        NOT NULL
  period_end_date   date        NOT NULL
  source            text        NOT NULL CHECK enum (3 values)
  outcome           text        NOT NULL CHECK enum (4 values)
  attempted_at      timestamptz NOT NULL
  notes             text        NULL
  created_at        timestamptz NOT NULL DEFAULT NOW()
  updated_at        timestamptz NOT NULL DEFAULT NOW()  -- maintained by trigger

  PRIMARY KEY (ticker, period_end_date, source)

CHECK enums (frozen — drift would silently subvert the validator join):

  source  ∈ {'fmp_historical', 'fmp_refresh', 'sec_companyfacts'}
  outcome ∈ {'yielded', 'empty', 'extract_none', 'fetch_failure'}

Index `(ticker, period_end_date)` supports the validator's per-ticker
+ per-period join.

Idempotent migration: `CREATE TABLE IF NOT EXISTS` + DROP-then-ADD on
each CHECK constraint + `CREATE OR REPLACE FUNCTION` + `DROP TRIGGER
IF EXISTS` then `CREATE TRIGGER`.

The validator's evidence-join is gated on `to_regclass(
'platform.fundamentals_period_source_evidence') IS NOT NULL`, so a
rollback gracefully degrades — the check skips the evidence join
entirely + the bucket's narrow semantic (< 2 filings + past grace)
keeps firing as today.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260602_0200"
down_revision: str | None = "20260602_0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Enum payloads kept as module-level tuples so the sentinel test can
# pin them; drift here is drift in the validator's join SQL.
_VALID_OUTCOMES = (
    "yielded",          # provider returned a row for this period
    "empty",            # FMP cascade fetched but no row for this period
    "extract_none",     # SEC companyfacts fetched but extract_period None
    "fetch_failure",    # HTTP 404 / 5xx / DataProviderOutage
)
_VALID_SOURCES = (
    "fmp_historical",     # historical_fundamentals_quarterly cascade
    "fmp_refresh",        # fundamentals_refresh cascade
    "sec_companyfacts",   # sec_fundamentals_fallback
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.fundamentals_period_source_evidence (
            ticker            text        NOT NULL,
            period_end_date   date        NOT NULL,
            source            text        NOT NULL,
            outcome           text        NOT NULL,
            attempted_at      timestamptz NOT NULL,
            notes             text,
            created_at        timestamptz NOT NULL DEFAULT NOW(),
            updated_at        timestamptz NOT NULL DEFAULT NOW(),
            CONSTRAINT fundamentals_period_source_evidence_pk
                PRIMARY KEY (ticker, period_end_date, source)
        )
        """
    )

    # outcome CHECK enum — DROP-then-ADD for idempotency.
    op.execute(
        "ALTER TABLE platform.fundamentals_period_source_evidence "
        "DROP CONSTRAINT IF EXISTS fundamentals_period_source_evidence_outcome_check"
    )
    valid_outcomes = ", ".join(f"'{v}'::text" for v in _VALID_OUTCOMES)
    op.execute(
        "ALTER TABLE platform.fundamentals_period_source_evidence "
        "ADD CONSTRAINT fundamentals_period_source_evidence_outcome_check "
        f"CHECK (outcome = ANY(ARRAY[{valid_outcomes}]))"
    )

    # source CHECK enum — DROP-then-ADD for idempotency.
    op.execute(
        "ALTER TABLE platform.fundamentals_period_source_evidence "
        "DROP CONSTRAINT IF EXISTS fundamentals_period_source_evidence_source_check"
    )
    valid_sources = ", ".join(f"'{v}'::text" for v in _VALID_SOURCES)
    op.execute(
        "ALTER TABLE platform.fundamentals_period_source_evidence "
        "ADD CONSTRAINT fundamentals_period_source_evidence_source_check "
        f"CHECK (source = ANY(ARRAY[{valid_sources}]))"
    )

    # Per-ticker + per-period index supports the validator join.
    op.execute(
        "CREATE INDEX IF NOT EXISTS "
        "fundamentals_period_source_evidence_ticker_period_idx "
        "ON platform.fundamentals_period_source_evidence "
        "(ticker, period_end_date)"
    )

    # Trigger function to maintain updated_at on UPSERT.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION platform.fundamentals_period_source_evidence_touch_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )

    op.execute(
        "DROP TRIGGER IF EXISTS fundamentals_period_source_evidence_updated_at_trg "
        "ON platform.fundamentals_period_source_evidence"
    )
    op.execute(
        """
        CREATE TRIGGER fundamentals_period_source_evidence_updated_at_trg
            BEFORE UPDATE ON platform.fundamentals_period_source_evidence
            FOR EACH ROW
            EXECUTE FUNCTION platform.fundamentals_period_source_evidence_touch_updated_at()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS fundamentals_period_source_evidence_updated_at_trg "
        "ON platform.fundamentals_period_source_evidence"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS platform.fundamentals_period_source_evidence_touch_updated_at()"
    )
    op.execute(
        "DROP INDEX IF EXISTS platform.fundamentals_period_source_evidence_ticker_period_idx"
    )
    op.execute(
        "DROP TABLE IF EXISTS platform.fundamentals_period_source_evidence"
    )
