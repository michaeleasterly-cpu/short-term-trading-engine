"""v2.2 — canonical 3-tier (issuers / securities / listings) reference-data shape.

Per financial-DB-expert verdict 2026-05-23 (FactSet Symbology, OpenFIGI allocation
rules, Bloomberg BSYM): `platform.ticker_classifications` is a LISTING-grained
table (one row per ticker × venue × country). At that grain, none of the
cross-vendor identity columns (cik / cusip / isin / figi) should be UNIQUE
because:

  cik              — identifies an ISSUER (legal entity). Many securities per CIK.
  cusip            — identifies a SECURITY (issue). Can have multiple listings.
  isin             — identifies a SECURITY across countries (= country + CUSIP).
  composite figi   — identifies a SECURITY within a country (aggregates venues).

GOOG vs GOOGL is the canonical proof: same issuer (Alphabet CIK 0001652044),
two distinct securities (Class A CUSIP 02079K305, Class C 02079K107), two
US listings — exactly the row-shape the bad UNIQUE constraints rejected.

## What this migration does

1. DROP the 4 partial UNIQUE indexes on ticker_classifications.{cusip, isin,
   cik, figi}. They encode a model that contradicts the canonical financial-data
   ontology and are the literal cause of the FMP profile backfill failures
   (2026-05-23 — failed 286s into the run on the first CUSIP collision).

2. KEEP non-unique indexes on the same 4 columns so lookups stay fast.
   (Postgres CREATE INDEX IF NOT EXISTS — partial UNIQUE drop doesn't auto-
   create a non-unique sibling.)

3. ADD CHECK constraints for FORMAT VALIDITY ONLY on the 3 ISO-format columns
   (no uniqueness implied; just shape validation):
     - cusip char(9): [0-9A-Z]{9} (no PUNCT; CUSIP charset)
     - isin char(12): 2-letter country prefix + 9-char alphanumeric + 1-digit check
     - figi char(12): per OpenFIGI allocation rules (excludes BS/BM/GG/GB/VG/GH/KY
                     prefixes; pos 3 = literal 'G'; charset excludes vowels)

4. CREATE 3 publishing views over the flat ticker_classifications table that
   give consumers the canonical 3-tier shape (issuers → securities → listings).
   These views are the contract for stelib's published API + for future internal
   consumers. When/if the storage IS eventually refactored into 3 physical
   tables, the views become tables, the public contract is unchanged.

Sources: OpenFIGI allocation rules <https://www.openfigi.com/assets/local/figi-allocation-rules.pdf>,
FactSet Symbology API <https://developer.factset.com/api-catalog/symbology-api>,
Bloomberg Open Symbology, Proof Engineering Security Master,
Databento Security Master.

Revision ID: 20260524_0800
Revises: 20260524_0701
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_0800"
down_revision: str | None = "20260524_0701"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # 1. Drop the 4 wrong-grain UNIQUE indexes.
    for col in ("cusip", "isin", "cik", "figi"):
        op.execute(
            f"DROP INDEX IF EXISTS platform.ticker_classifications_{col}_uniq"
        )

    # 2. Add NON-UNIQUE covering indexes for lookup speed.
    for col in ("cusip", "isin", "cik", "figi"):
        op.execute(
            f"""
            CREATE INDEX IF NOT EXISTS ticker_classifications_{col}_idx
            ON platform.ticker_classifications ({col})
            WHERE {col} IS NOT NULL
            """
        )

    # 3. Add CHECK constraints for format validity only (NOT uniqueness).
    # Wrapped in DO $$ ... EXCEPTION for partial-replay idempotency
    # (Postgres has no ADD CONSTRAINT IF NOT EXISTS for CHECK).
    op.execute(
        """
        DO $$
        BEGIN
            ALTER TABLE platform.ticker_classifications
                ADD CONSTRAINT ticker_classifications_cusip_format_chk
                CHECK (cusip IS NULL OR cusip ~ '^[0-9A-Z]{9}$');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            ALTER TABLE platform.ticker_classifications
                ADD CONSTRAINT ticker_classifications_isin_format_chk
                CHECK (isin IS NULL OR isin ~ '^[A-Z]{2}[0-9A-Z]{9}[0-9]$');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
        """
    )
    # FIGI per OpenFIGI allocation rules: 12 chars total.
    # Pos 1-2: consonants (no vowels), but NOT in the forbidden country-prefix
    # set BS/BM/GG/GB/VG/GH/KY.
    # Pos 3: literal 'G'.
    # Pos 4-11: alphanumeric excluding vowels.
    # Pos 12: numeric check digit.
    op.execute(
        r"""
        DO $$
        BEGIN
            ALTER TABLE platform.ticker_classifications
                ADD CONSTRAINT ticker_classifications_figi_format_chk
                CHECK (
                    figi IS NULL
                    OR (
                        figi ~ '^[BCDFGHJKLMNPQRSTVWXZ]{2}G[BCDFGHJKLMNPQRSTVWXYZ0-9]{8}[0-9]$'
                        AND substring(figi, 1, 2) NOT IN ('BS','BM','GG','GB','VG','GH','KY')
                    )
                );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
        """
    )

    # 4. Create the 3-tier publishing views (canonical financial-data shape).
    # Schema mirrors FactSet S/R/L + OpenFIGI 3-tier. Future storage refactor
    # turns these views into physical tables; the contract stays unchanged.
    op.execute(
        """
        CREATE OR REPLACE VIEW platform.issuers_v AS
        SELECT DISTINCT
            cik                              AS issuer_id,
            cik,
            current_legal_name               AS legal_name,
            country                          AS country_of_incorp
        FROM platform.ticker_classifications
        WHERE cik IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE OR REPLACE VIEW platform.securities_v AS
        SELECT DISTINCT ON (COALESCE(cusip, isin, figi))
            COALESCE(cusip, isin, figi)      AS security_id,
            cusip,
            isin,
            figi                             AS composite_figi,
            asset_class                      AS security_type,
            cik                              AS issuer_id
        FROM platform.ticker_classifications
        WHERE cusip IS NOT NULL OR isin IS NOT NULL OR figi IS NOT NULL
        ORDER BY COALESCE(cusip, isin, figi), updated_at DESC
        """
    )
    op.execute(
        """
        CREATE OR REPLACE VIEW platform.listings_v AS
        SELECT
            id                               AS listing_id,
            COALESCE(cusip, isin, figi)      AS security_id,
            cik                              AS issuer_id,
            current_ticker                   AS ticker,
            current_exchange                 AS exchange,
            country,
            status,
            ipo_venue,
            discovery_source,
            updated_at
        FROM platform.ticker_classifications
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS platform.listings_v")
    op.execute("DROP VIEW IF EXISTS platform.securities_v")
    op.execute("DROP VIEW IF EXISTS platform.issuers_v")

    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT IF EXISTS ticker_classifications_figi_format_chk"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT IF EXISTS ticker_classifications_isin_format_chk"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT IF EXISTS ticker_classifications_cusip_format_chk"
    )

    for col in ("cusip", "isin", "cik", "figi"):
        op.execute(f"DROP INDEX IF EXISTS platform.ticker_classifications_{col}_idx")
        op.execute(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS ticker_classifications_{col}_uniq
            ON platform.ticker_classifications ({col})
            WHERE {col} IS NOT NULL
            """
        )
