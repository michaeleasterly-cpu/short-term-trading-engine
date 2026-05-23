"""v2.2 Phase P2 — add TKR-14 PK + cross-vendor identity columns to ticker_classifications.

Per v2.2 spec §1.2 (TKR-14 encoding), §1.9 (cross-vendor columns), §2 (schema target);
per v2.2 plan P2 (this migration is the first of two).

Adds the following to `platform.ticker_classifications`:
- `id text` — TKR-14 smart-key; nullable initially, populated by parent_resolver
  + one-shot backfill in v2.2 P5; then `ALTER COLUMN SET NOT NULL` + PK swap in P5.
- `figi char(12)` — OpenFIGI US Composite FIGI; UNIQUE-NULLABLE.
- `cusip char(9)` — FMP-derived; UNIQUE-NULLABLE; null for non-NA listings.
- `isin char(12)` — FMP-derived; UNIQUE-NULLABLE; US ISIN body = CUSIP.
- `current_ticker text` — denormalized convenience for live "current symbol" queries;
  initially populated from the existing `ticker` column (PK) in the same migration.
- `current_exchange text` — mutable; XNYS / XNAS / etc.; FMP-derived.
- `current_legal_name text` — mutable; M&A / rebrand churn.
- `gics_sector text` — mutable; ~4%/yr reclassification rate.
- `ipo_venue text` — at-mint snapshot (N/Q/A/B/O/X/Z); used by TKR-14 mint pos 4.
- `discovery_source text` — at-mint snapshot (F/S/A/O); used by TKR-14 mint pos 7.
- `cik text` — SEC CIK for US issuers; UNIQUE-NULLABLE; used by TKR-14 issuer-hash seed.
- `updated_at timestamptz` — refresh-on-write marker.

Adds the following constraints (all nullable initially; flipped to NOT NULL/PK in P5
after backfill):
- CHECK constraint on `id` regex (per v2.2 spec §1.2): only enforced for non-null values.
- Partial UNIQUE on `(current_ticker) WHERE status IN ('active','active_when_issued')`.
- Partial UNIQUE on `(figi) WHERE figi IS NOT NULL`.
- Partial UNIQUE on `(cusip) WHERE cusip IS NOT NULL`.
- Partial UNIQUE on `(isin) WHERE isin IS NOT NULL`.
- Partial UNIQUE on `(cik) WHERE cik IS NOT NULL`.

Adds expression indexes for the TKR-14 filter patterns (per v2.2 spec §6):
- `(substring(id, 1, 2))` — country segment
- `(substring(id, 3, 1))` — asset class
- `(substring(id, 4, 1))` — IPO venue
- `(substring(id, 5, 2))` — discovery YY
- `(substring(id, 7, 1))` — discovery source

Same-migration data move: `UPDATE platform.ticker_classifications SET current_ticker = ticker`
(populates the new column from the existing PK; takes ~13K rows, fast).

**This migration does NOT flip `id` to NOT NULL or change the PK.** That happens in
the P5 migration after `scripts/ops.py --stage tkr14_backfill` populates `id` for
all rows.

**This migration does NOT change any FK relationships.** The 14 ticker-keyed NOT-VALID
FKs from v2.1 Phase 2 remain in place as intermediate scaffolding; they get retired
in P9 after the `classification_id`-keyed FKs are validated.

Pre-flight gates (per v2.2 plan P2.2):
- v2.2 spec + plan merged (PR #324 + #325).
- P1 (DFCR + FeedTrigger.EVENT_DRIVEN) MAY land before or after this migration; this
  migration has no dependency on the FeedTrigger or OpenFIGI adapter being live.
- Snapshot `ticker_classifications` via `bash scripts/run_db_snapshots.sh ticker_classifications`
  before applying.

Revision ID: 20260524_0000
Revises: 20260523_0701
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_0000"
down_revision: str | None = "20260523_0701"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


# Per v2.2 spec §1.2: the canonical TKR-14 regex. Mirrors tpcore.identity.tkr14.TKR14_REGEX
# (kept in lock-step; if you change one, change the other; a sentinel test asserts equality).
_TKR14_REGEX = (
    r"^[A-Z]{2}"                    # pos 1-2: country (ISO 3166-1 alpha-2)
    r"[SPEFRTAUWN]"                 # pos 3: asset class
    r"[NQABOXZ]"                    # pos 4: IPO venue
    r"[0-9]{2}"                     # pos 5-6: discovery year YY
    r"[FSAO]"                       # pos 7: discovery source
    r"[0-9A-HJ-KM-NP-TV-Z]{5}"      # pos 8-12: issuer hash (Crockford base32; no I/L/O/U)
    r"[0-9]{2}"                     # pos 13-14: ISO 7064 Mod-97-10 check digits
    r"$"
)


def upgrade() -> None:
    # ── 1. Add the new columns (all nullable initially) ─────────────
    op.execute(
        """
        ALTER TABLE platform.ticker_classifications
            ADD COLUMN IF NOT EXISTS id                 text,
            ADD COLUMN IF NOT EXISTS figi               char(12),
            ADD COLUMN IF NOT EXISTS cusip              char(9),
            ADD COLUMN IF NOT EXISTS isin               char(12),
            ADD COLUMN IF NOT EXISTS current_ticker     text,
            ADD COLUMN IF NOT EXISTS current_exchange   text,
            ADD COLUMN IF NOT EXISTS current_legal_name text,
            ADD COLUMN IF NOT EXISTS gics_sector        text,
            ADD COLUMN IF NOT EXISTS ipo_venue          text,
            ADD COLUMN IF NOT EXISTS discovery_source   text,
            ADD COLUMN IF NOT EXISTS cik                text,
            ADD COLUMN IF NOT EXISTS updated_at         timestamptz NOT NULL DEFAULT now()
        """
    )

    # ── 2. Same-migration data move: populate current_ticker from existing PK ──
    op.execute(
        "UPDATE platform.ticker_classifications SET current_ticker = ticker WHERE current_ticker IS NULL"
    )

    # ── 3. CHECK constraint on TKR-14 id regex (applies WHERE id IS NOT NULL) ──
    # Postgres CHECK with NULL → NULL → row passes. So this implicitly only enforces
    # for non-null values.
    op.execute(
        f"""
        ALTER TABLE platform.ticker_classifications
            ADD CONSTRAINT ticker_classifications_id_tkr14_regex_chk
            CHECK (id IS NULL OR id ~ '{_TKR14_REGEX}')
        """
    )

    # ── 4. Partial UNIQUE constraints ────────────────────────────────
    # current_ticker active-only uniqueness — allows historical delisted rows
    # to share a ticker with a future re-IPO.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ticker_classifications_current_ticker_active_uniq
            ON platform.ticker_classifications (current_ticker)
            WHERE status IN ('active', 'active_when_issued')
        """
    )
    # Cross-vendor identifier uniqueness (FIGI / CUSIP / ISIN / CIK)
    for col in ("figi", "cusip", "isin", "cik"):
        op.execute(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS ticker_classifications_{col}_uniq
                ON platform.ticker_classifications ({col})
                WHERE {col} IS NOT NULL
            """
        )

    # ── 5. Expression indexes for TKR-14 filter patterns ─────────────
    # Per v2.2 spec §6 — supports `WHERE substring(id, ...) = ...` queries.
    for col_name, segment in (
        ("country", "substring(id, 1, 2)"),
        ("asset_class", "substring(id, 3, 1)"),
        ("ipo_venue", "substring(id, 4, 1)"),
        ("discovery_yy", "substring(id, 5, 2)"),
        ("discovery_src", "substring(id, 7, 1)"),
    ):
        op.execute(
            f"""
            CREATE INDEX IF NOT EXISTS ticker_classifications_tkr14_{col_name}_idx
                ON platform.ticker_classifications (({segment}))
            """
        )


def downgrade() -> None:
    # Drop in reverse order. CHECK constraints + indexes first; then columns.
    for col_name in ("country", "asset_class", "ipo_venue", "discovery_yy", "discovery_src"):
        op.execute(
            f"DROP INDEX IF EXISTS platform.ticker_classifications_tkr14_{col_name}_idx"
        )
    op.execute(
        "DROP INDEX IF EXISTS platform.ticker_classifications_current_ticker_active_uniq"
    )
    for col in ("figi", "cusip", "isin", "cik"):
        op.execute(
            f"DROP INDEX IF EXISTS platform.ticker_classifications_{col}_uniq"
        )
    op.execute(
        """
        ALTER TABLE platform.ticker_classifications
            DROP CONSTRAINT IF EXISTS ticker_classifications_id_tkr14_regex_chk
        """
    )
    op.execute(
        """
        ALTER TABLE platform.ticker_classifications
            DROP COLUMN IF EXISTS updated_at,
            DROP COLUMN IF EXISTS cik,
            DROP COLUMN IF EXISTS discovery_source,
            DROP COLUMN IF EXISTS ipo_venue,
            DROP COLUMN IF EXISTS gics_sector,
            DROP COLUMN IF EXISTS current_legal_name,
            DROP COLUMN IF EXISTS current_exchange,
            DROP COLUMN IF EXISTS current_ticker,
            DROP COLUMN IF EXISTS isin,
            DROP COLUMN IF EXISTS cusip,
            DROP COLUMN IF EXISTS figi,
            DROP COLUMN IF EXISTS id
        """
    )
