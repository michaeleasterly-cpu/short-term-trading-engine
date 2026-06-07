"""ETF-attributes satellite — physical entity separation for ETFs (db-architect Variant A).

Spec/decision: operator decision (data-layer REBUILD arc) — ETFs are a distinct
physical entity and get their own table; corp-actions/prices stay shared (all
asset classes have them); fundamentals stays stock/reit-scoped. This migration
does ONLY the ETF satellite, in TRANSITION MODE.

TRANSITION MODE: this migration CREATES + BACKFILLS the satellite and adds the
FK back to the spine, but DELIBERATELY does NOT drop the spine etf_* columns
(``ticker_classifications.etf_inverse`` / ``etf_leverage`` / ``etf_category``).
Consumers ``tpcore/data/classify_tickers.py`` + ``scripts/ops.py`` still read the
spine columns; the column-drop + consumer-rewire is a tracked follow-up. Both
copies coexist until that follow-up flips the readers.

## Schema rationale (controls-audit §13 #11)

Readers (named code paths that will query the new table):
  - ``tpcore/data/classify_tickers.py`` — ETF classification maintenance (today
    reads/writes the spine etf_* columns; the tracked follow-up rewires it to
    read/write this satellite).
  - ``scripts/ops.py`` — ETF-maintenance stage (same spine→satellite follow-up).
  - Future ETF-filter consumers (inverse/leverage/category screens) — read this
    satellite directly rather than the spine.

Writers (canonical writer; single-writer unless justified):
  - ``tpcore/data/classify_tickers.py`` — the single canonical writer of ETF
    attributes (post-rewire). The seed (``scripts/rebuild_identity_seed.py``)
    populates it deterministically from the spine on a clean re-seed; that is a
    bootstrap path, not a competing steady-state writer.

Existing-table alternative considered:
  - ``platform.ticker_classifications`` (the spine): the etf_* columns live here
    today. Rejected as the long-term home per the operator's physical-entity
    decision — ETF-only attributes pollute the universal securities spine that
    every asset class (stock/etf/spac/fund/adr/reit) shares. A satellite keyed
    1:1 on ``classification_id`` keeps ETF-specific columns off the spine while
    preserving the identity chain (ticker + date → classification_id → CIK).

Why not extend the existing identity / lifecycle substrate?
  - This is NOT a sidecar/evidence/quarantine table — it carries no
    "data-we're-unsure-about" provenance; it is a 1:1 attribute satellite of an
    existing, fully-classified entity (asset_class IN ('etf','etn')). It does
    not duplicate the SCD-2 ``ticker_history`` mechanics or any BEFORE INSERT
    trigger logic; it simply relocates already-classified ETF columns to their
    own physical entity, FK'd to ``ticker_classifications.id`` exactly as
    ``prices_daily`` / ``fundamentals_quarterly`` are.

Live introspection (2026-06-07, alembic head 20260604_0600):
  * ``ticker_classifications.id`` is ``text``, every row exactly 14 chars
    (19,004 rows). The satellite PK/FK is ``char(14)`` per the settled design;
    a ``char(14)`` child FK to a ``text`` PK is valid (string-family equality)
    and all ids are fixed-width 14, so no padding mismatch.
  * asset_class distribution: stock 10758, etf 4890, spac 1330, fund 1120,
    adr 715, reit 191. etn = 0 today, but the backfill + gate probes include
    'etn' for forward-safety (ETNs are ETF-adjacent and should they appear they
    belong in this satellite).
  * platform.etf_attributes did not exist pre-apply.
"""
from __future__ import annotations

from alembic import op

revision = "20260607_0100"
down_revision = "20260604_0600"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Satellite table — PK == FK == classification_id (1:1 with the spine).
    #    Idempotent (IF NOT EXISTS) so the replay-from-zero invariant holds.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.etf_attributes (
            classification_id char(14) PRIMARY KEY,
            etf_inverse       boolean,
            etf_leverage      numeric,
            etf_category      text,
            updated_at        timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # 2) Backfill from the spine — ETF/ETN rows only. ON CONFLICT DO NOTHING so
    #    a re-run is a no-op (idempotent).
    op.execute(
        """
        INSERT INTO platform.etf_attributes
            (classification_id, etf_inverse, etf_leverage, etf_category)
        SELECT id, etf_inverse, etf_leverage, etf_category
        FROM platform.ticker_classifications
        WHERE asset_class IN ('etf', 'etn')
        ON CONFLICT (classification_id) DO NOTHING
        """
    )

    # 3) FK back to the spine — NOT VALID then VALIDATE (the audit-before-alter
    #    pattern; the backfill above sources every row from the spine so there
    #    can be no orphan, but VALIDATE makes the guarantee explicit + future
    #    inserts are checked). ON DELETE RESTRICT protects ETF attributes from
    #    a spine delete; ON UPDATE CASCADE propagates a (rare) id change.
    #    Guarded by a NOT-EXISTS check so the migration is idempotent.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'etf_attributes_classification_id_fk'
                  AND conrelid = 'platform.etf_attributes'::regclass
            ) THEN
                ALTER TABLE platform.etf_attributes
                    ADD CONSTRAINT etf_attributes_classification_id_fk
                    FOREIGN KEY (classification_id)
                    REFERENCES platform.ticker_classifications(id)
                    ON UPDATE CASCADE ON DELETE RESTRICT
                    NOT VALID;
            END IF;
        END $$;
        """
    )
    op.execute(
        "ALTER TABLE platform.etf_attributes "
        "VALIDATE CONSTRAINT etf_attributes_classification_id_fk"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE platform.etf_attributes "
        "DROP CONSTRAINT IF EXISTS etf_attributes_classification_id_fk"
    )
    op.execute("DROP TABLE IF EXISTS platform.etf_attributes")
