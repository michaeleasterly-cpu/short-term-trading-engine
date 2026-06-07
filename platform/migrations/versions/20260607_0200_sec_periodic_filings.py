"""SEC periodic-filings substrate — authoritative index for the
fundamentals_quarterly_completeness full fix.

This migration creates ``platform.sec_periodic_filings``: an authoritative,
single-source index of the SEC periodic filings (10-Q / 10-K / 20-F / 40-F and
their /A amendments) that establish the existence of a company's fiscal-period
report. It is the substrate the fundamentals_quarterly_completeness gate consults
to decide whether a missing fundamentals row is a genuine ingest gap (an SEC
periodic filing exists for the period but we never persisted the fundamentals)
versus a true non-event (no periodic filing → nothing to expect).

It is an AUTHORITATIVE single-source index with an FK + BEFORE INSERT trigger
into the identity substrate (``classification_id`` → ``ticker_classifications``),
NOT a sidecar / evidence / quarantine table. It carries no
"data-we're-unsure-about" provenance and duplicates none of the SCD-2
``ticker_history`` mechanics beyond the canonical classification_id assignment
trigger that every identity-bearing platform table uses.

## Schema rationale (controls-audit §13 #11)

Readers (named code paths that will query the new table):
  - ``fundamentals_quarterly_completeness`` safety gate — the data-acceptance
    check that, today, cannot distinguish "company filed a 10-Q/10-K we failed
    to ingest" from "company has no periodic filing for this period". It reads
    this table (via one shared helper, below) to make that distinction.
  - ``fundamentals_refresh`` healer — the self-heal path that re-fetches missing
    fundamentals; it reads the SAME shared helper to know which (ticker, period)
    pairs to chase. One shared helper, two readers (gate + healer), so the gate's
    expectation and the healer's worklist can never diverge.

Writers (canonical writer; single-writer unless justified):
  - ``_stage_backfill_sec_metadata`` — SINGLE canonical writer. It persists the
    periodic-filing rows that the existing SEC-metadata ingest ALREADY fetches
    and then discards. ZERO new SEC fetch is introduced by this table: the rows
    are already on the wire during the metadata pass; this writer simply lands
    the periodic-form subset that was previously dropped on the floor.

Existing-table alternative considered (why none is the home):
  - ``platform.sec_document_type_history`` — a per-(entity, form_type) COUNT
    histogram. It has NO per-filing dates (report_date / filing_date) and no
    accession_number, so it cannot answer "is there a 10-Q for FY2025-Q3?" — it
    only says "this CIK has filed N 10-Qs ever". Rejected: wrong grain.
  - ``platform.fundamentals_quarterly`` — this IS the table the completeness gate
    checks. Using it as the source-of-expectation would be circular (the gate
    would conclude a row is missing iff the row is missing). Rejected: circular.
  - ``platform.ticker_lifecycle_events`` — a different domain: TERMINAL lifecycle
    events (Form 25 delisting / Form 15 deregistration). Its ``report_date``
    means the *claimed-effective-date* of the lifecycle event, NOT a fiscal
    period-end. Overloading it with periodic-report filings would conflate two
    semantically distinct date meanings. Rejected: different domain + different
    report_date semantics.

Why not extend the existing identity / lifecycle substrate?
  - The grain (one row per SEC periodic accession) and the columns
    (form_type / report_date=fiscal-period-end / filing_date / accession_number)
    have no home in ticker_history / ticker_classifications (identity SCD-2) or
    in the lifecycle / document-type substrate (terminal events / count
    histograms). It is keyed on the identity chain exactly like every other
    identity-bearing table — ``classification_id`` FK'd to
    ``ticker_classifications.id`` with the canonical half-open SCD-2 BEFORE INSERT
    trigger (copied verbatim from 20260604_0100) — so it participates in the same
    ticker + date → classification_id → CIK chain rather than re-implementing it.

Live introspection (2026-06-07, alembic head 20260607_0100):
  * ``ticker_classifications.id`` is ``text`` (no max length). The child FK column
    ``classification_id`` is ``text`` to match exactly (string-family equality).
  * ``ticker_history`` carries (classification_id text, ticker text,
    valid_from date, valid_to date) — the trigger reads it with the canonical
    half-open predicate.
  * ``platform.sec_periodic_filings`` did not exist pre-apply; the table is
    created empty (0 rows). The FK is added NOT VALID then VALIDATE'd — trivially
    clean because the table is empty.

Revision ID: 20260607_0200
Revises: 20260607_0100
Create Date: 2026-06-07
"""
from __future__ import annotations

from alembic import op

revision = "20260607_0200"
down_revision = "20260607_0100"
branch_labels = None
depends_on = None

FN_NAME = "tg_set_classification_id_sec_periodic_filings"
TRIGGER_NAME = "trg_set_classification_id_sec_periodic_filings"
FK_NAME = "sec_periodic_filings_classification_id_fk"


def upgrade() -> None:
    # 1) Table — authoritative single-source index of SEC periodic filings.
    op.execute(
        """
        CREATE TABLE platform.sec_periodic_filings (
            id                bigserial   PRIMARY KEY,
            cik               text        NOT NULL,
            classification_id text,
            ticker            text        NOT NULL,
            form_type         text        NOT NULL,
            report_date       date,
            filing_date       date        NOT NULL,
            accession_number  text        NOT NULL,
            ingested_at       timestamptz NOT NULL DEFAULT NOW(),
            CONSTRAINT sec_periodic_filings_form_type_chk
                CHECK (form_type = ANY (ARRAY[
                    '10-Q','10-K','20-F','40-F',
                    '10-Q/A','10-K/A','20-F/A','40-F/A'
                ]::text[]))
        )
        """
    )

    # 2) Indexes.
    op.execute(
        "CREATE UNIQUE INDEX ux_sec_periodic_filings_cik_accession "
        "ON platform.sec_periodic_filings (cik, accession_number)"
    )
    op.execute(
        "CREATE INDEX ix_sec_periodic_filings_cid_report "
        "ON platform.sec_periodic_filings (classification_id, report_date) "
        "WHERE report_date IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_sec_periodic_filings_cik_filing "
        "ON platform.sec_periodic_filings (cik, filing_date DESC)"
    )
    op.execute(
        "CREATE INDEX ix_sec_periodic_filings_classification_id "
        "ON platform.sec_periodic_filings (classification_id)"
    )

    # 3) Canonical half-open SCD-2 classification_id BEFORE INSERT trigger.
    #    Body shape copied VERBATIM from 20260604_0100 (the half-open form):
    #      - only fires when NEW.classification_id IS NULL,
    #      - as_of = NEW.filing_date,
    #      - valid_from <= as_of AND (valid_to IS NULL OR as_of < valid_to),
    #      - ORDER BY valid_from DESC LIMIT 1.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION platform.{FN_NAME}()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.classification_id IS NULL THEN
                SELECT classification_id INTO NEW.classification_id
                FROM platform.ticker_history
                WHERE ticker = NEW.ticker
                  AND valid_from <= NEW.filing_date
                  AND (valid_to IS NULL OR NEW.filing_date < valid_to)
                ORDER BY valid_from DESC
                LIMIT 1;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"CREATE TRIGGER {TRIGGER_NAME} "
        f"BEFORE INSERT ON platform.sec_periodic_filings "
        f"FOR EACH ROW EXECUTE FUNCTION platform.{FN_NAME}()"
    )

    # 4) Identity FK — NOT VALID then VALIDATE (the project's standard two-step).
    #    Table is empty, so VALIDATE is trivially clean.
    #      ON UPDATE CASCADE  — ticker_classifications.id renames propagate.
    #      ON DELETE RESTRICT — protect this index; never silent cascade-delete.
    op.execute(
        f"ALTER TABLE platform.sec_periodic_filings "
        f"ADD CONSTRAINT {FK_NAME} "
        f"FOREIGN KEY (classification_id) REFERENCES platform.ticker_classifications(id) "
        f"ON UPDATE CASCADE ON DELETE RESTRICT NOT VALID"
    )
    op.execute(
        f"ALTER TABLE platform.sec_periodic_filings VALIDATE CONSTRAINT {FK_NAME}"
    )


def downgrade() -> None:
    # Full round-trip. DROP TABLE cascades the indexes, FK, and CHECK.
    op.execute(
        f"DROP TRIGGER IF EXISTS {TRIGGER_NAME} ON platform.sec_periodic_filings"
    )
    op.execute(f"DROP FUNCTION IF EXISTS platform.{FN_NAME}()")
    op.execute("DROP TABLE IF EXISTS platform.sec_periodic_filings")
