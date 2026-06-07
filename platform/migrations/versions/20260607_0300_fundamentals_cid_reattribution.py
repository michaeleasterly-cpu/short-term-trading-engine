"""Identity-substrate REPAIR — re-attribute NULL-``classification_id``
``fundamentals_quarterly`` rows via a three-tier evidence cascade.

This is a DATA-REPAIR migration (controls-audit §13 #11: it creates NO new
table and adds NO schema object — it repairs the identity stamp on existing
rows of an existing identity-bearing table). It is NOT a sidecar / evidence /
quarantine table; it writes only ``platform.fundamentals_quarterly.classification_id``
on rows where that column is currently NULL.

## Root cause — the as-of asymmetry

``platform.fundamentals_quarterly`` carries the canonical BEFORE-INSERT
classification_id trigger, but that trigger anchors the SCD-2 ``ticker_history``
lookup at ``period_end_date`` (the fiscal-period-end), NOT ``filing_date``.
For a company that IPO'd AFTER one of its reported fiscal periods ended, the
``period_end_date`` of an early restated/historical quarter falls BEFORE the
ticker's first ``ticker_history`` window (the post-IPO window). The half-open
predicate ``valid_from <= as_of AND (valid_to IS NULL OR as_of < valid_to)``
then matches NO window, so the trigger leaves ``classification_id`` NULL.

Live introspection (2026-06-07, head 20260607_0200): 17,056 rows (9.2% of
184,505), 2,376 distinct tickers carry ``classification_id IS NULL``. Of those
tickers, 92 are RECYCLED (the ticker string maps to >1 distinct
classification_id across ``ticker_history`` — a delisted-then-reused symbol),
accounting for 587 of the NULL rows; the remaining 2,284 tickers are
SOLE-ENTITY (exactly one classification_id).

These NULL stamps produce ~331 confirmed false-positive "missing" reportDates
in the ``fundamentals_quarterly_completeness`` safety gate, because that gate's
``have`` set keys on ``classification_id`` (a NULL-cid fundamentals row is not
credited toward the issuer it actually belongs to).

``filing_date`` alone does NOT fix this: ``handlers.py`` sets ``filing=pe`` for
62% of the NULL rows, so a filing_date-only re-attribution resolves <6%. Hence
the three-tier cascade, highest-evidence first, each pass scoped to
``WHERE classification_id IS NULL`` (idempotent + cumulative).

## The three tiers (highest evidence first)

* **Tier 1 — SEC co-attribution.** Set cid from ``platform.sec_periodic_filings``
  matched on ``ticker`` AND ``report_date = period_end_date``, ONLY when that
  (ticker, period_end_date) pair resolves to EXACTLY ONE distinct non-NULL
  ``spf.classification_id``. Ambiguous (>1 distinct) pairs are skipped. This is
  SEC-authoritative identity at the period grain.

* **Tier 2 — reliable filing_date window.** Set cid from the ``ticker_history``
  window that contains ``filing_date`` (same half-open predicate the trigger
  uses), ONLY when ``filing_date <> period_end_date`` (so the filing_date is a
  real, distinct filing date — not the ``filing=pe`` sentinel) AND EXACTLY ONE
  window matches. For a recycled ticker this attributes each period to the
  entity that actually held the symbol at filing time (verified live: STRC's
  2022-12-31 row -> the 2023-2024 entity; its 2025-06-30 row -> the 2025+ entity).

* **Tier 3 — sole-entity identity.** For tickers that map to EXACTLY ONE
  classification_id across ALL of ``ticker_history`` (no reuse), set that single
  cid regardless of date. A sole-entity ticker has no ambiguity to resolve, so
  the date is irrelevant. This tier CANNOT touch a recycled ticker (the
  ``HAVING count(DISTINCT classification_id) = 1`` guard excludes them), so it
  can never misattribute a recycled-ticker period to the wrong holder.

## Deliberately left NULL (do NOT guess)

The recycled-ticker rows that Tiers 1+2 cannot resolve (no SEC co-attribution
AND filing_date is the ``filing=pe`` sentinel or falls in a window-gap / before
all windows) stay ``classification_id IS NULL``. Modeled live: 539 rows (all on
the 92 recycled tickers). These are CORRECT-to-leave-NULL: attributing them
would be a guess across an entity boundary, exactly the recycled-ticker
contamination the identity substrate exists to prevent. The completeness gate's
companion change (FIX 2) makes the gate immune to residual NULL rows without
guessing their identity.

Predicted live row counts (modeled via SELECT before apply):
  Tier 1 ~ 304 - Tier 2 ~ (incremental after T1) - Tier 3 ~ 16,469
  -> NULL before 17,056 -> NULL after ~ 539.

## downgrade()

Intentional NO-OP (house style, cf. 20260524_1702 / 20260524_1701). A clean
un-attribution is impossible: this migration writes the SAME canonical cid the
trigger WOULD have written had it anchored on filing_date, so blanket-NULLing
all cids on downgrade would (a) destroy correct stamps that pre-existed this
migration and (b) re-introduce the false-positive defect. The repair is
forward-only; the rows touched are identifiable post-hoc only as "non-NULL cid
on rows the trigger left NULL", which is not a reversible delta.

Revision ID: 20260607_0300
Revises: 20260607_0200
Create Date: 2026-06-07
"""
from __future__ import annotations

from alembic import op

revision = "20260607_0300"
down_revision = "20260607_0200"
branch_labels = None
depends_on = None

# Supabase chunked-DML mandate: never one multi-thousand-row UPDATE. We bound
# each statement to a CTID slice so WAL recycles incrementally (cf. the
# 20260524_0700 prices_daily incident). 5,000 rows/chunk is comfortably under
# the pooler's appetite for a 17K-row table.
_CHUNK = 5000

# Tier 1 - SEC co-attribution. EXACTLY ONE distinct non-NULL spf.classification_id
# for (ticker, report_date = period_end_date).
_TIER1 = """
    UPDATE platform.fundamentals_quarterly fq
    SET classification_id = (
        SELECT DISTINCT spf.classification_id
        FROM platform.sec_periodic_filings spf
        WHERE spf.ticker = fq.ticker
          AND spf.report_date = fq.period_end_date
          AND spf.classification_id IS NOT NULL
    )
    WHERE fq.ctid = ANY(
        SELECT c.ctid FROM platform.fundamentals_quarterly c
        WHERE c.classification_id IS NULL
          AND (
            SELECT count(DISTINCT spf.classification_id)
            FROM platform.sec_periodic_filings spf
            WHERE spf.ticker = c.ticker
              AND spf.report_date = c.period_end_date
              AND spf.classification_id IS NOT NULL
          ) = 1
        LIMIT {chunk}
    )
"""

# Tier 2 - reliable filing_date window. EXACTLY ONE ticker_history window contains
# filing_date, and filing_date is a real filing date (<> period_end_date).
_TIER2 = """
    UPDATE platform.fundamentals_quarterly fq
    SET classification_id = (
        SELECT th.classification_id
        FROM platform.ticker_history th
        WHERE th.ticker = fq.ticker
          AND th.valid_from <= fq.filing_date
          AND (th.valid_to IS NULL OR fq.filing_date < th.valid_to)
        ORDER BY th.valid_from DESC
        LIMIT 1
    )
    WHERE fq.ctid = ANY(
        SELECT c.ctid FROM platform.fundamentals_quarterly c
        WHERE c.classification_id IS NULL
          AND c.filing_date <> c.period_end_date
          AND (
            SELECT count(*) FROM platform.ticker_history th
            WHERE th.ticker = c.ticker
              AND th.valid_from <= c.filing_date
              AND (th.valid_to IS NULL OR c.filing_date < th.valid_to)
          ) = 1
        LIMIT {chunk}
    )
"""

# Tier 3 - sole-entity identity. Ticker maps to EXACTLY ONE classification_id
# across ALL ticker_history (no reuse -> no ambiguity -> date-independent).
_TIER3 = """
    UPDATE platform.fundamentals_quarterly fq
    SET classification_id = sole.classification_id
    FROM (
        SELECT ticker, min(classification_id) AS classification_id
        FROM platform.ticker_history
        GROUP BY ticker
        HAVING count(DISTINCT classification_id) = 1
    ) sole
    WHERE fq.ctid = ANY(
        SELECT c.ctid FROM platform.fundamentals_quarterly c
        WHERE c.classification_id IS NULL
          AND c.ticker = sole.ticker
        LIMIT {chunk}
    )
      AND fq.ticker = sole.ticker
"""


def _run_chunked(sql_template: str) -> None:
    """Apply one tier in CTID-bounded chunks until no row is left to touch.

    Each chunk is its own statement (the migration runs in alembic's
    transaction; the chunking bounds the per-statement WAL, not the txn). A
    chunk that updates 0 rows ends the loop.
    """
    bind = op.get_bind()
    sql = sql_template.format(chunk=_CHUNK)
    while True:
        res = bind.exec_driver_sql(sql)
        if res.rowcount == 0:
            break


def upgrade() -> None:
    bind = op.get_bind()
    # Defensive: suppress the BEFORE-INSERT/UPDATE classification_id trigger
    # during these UPDATEs. The trigger fires on INSERT only, but replica role
    # is the project's standard belt-and-braces guard for data-repair writes so
    # no identity trigger can re-derive (and re-NULL) a cid we are stamping.
    op.execute("SET LOCAL session_replication_role = 'replica'")

    _run_chunked(_TIER1)
    _run_chunked(_TIER2)
    _run_chunked(_TIER3)

    # Restore default replication role within the migration txn.
    op.execute("SET LOCAL session_replication_role = 'origin'")
    _ = bind


def downgrade() -> None:
    # Intentional NO-OP. See module docstring "downgrade()": this repair writes
    # the canonical cid the trigger would have written had it anchored on
    # filing_date; un-attribution is not a reversible delta (would destroy
    # pre-existing correct stamps and re-introduce the false-positive defect).
    pass
