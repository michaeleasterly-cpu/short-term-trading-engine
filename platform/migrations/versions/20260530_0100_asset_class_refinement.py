"""asset_class refinement — expand 4 → 10 classes + instrument_subtype

Revision ID: 20260530_0100
Revises: 20260527_0400
Create Date: 2026-05-30

2026-05-30 quantitative finance expert review (see
``docs/superpowers/specs/2026-05-30-asset-class-refinement.md``):

  The existing 4-class taxonomy (``stock``, ``etf``, ``spac``, ``fund``)
  conflates instruments that behave very differently:

    * ``stock`` hides ADRs (different 20-F filing cadence — catalyst
      engine's earnings calendar is wrong on them), preferreds (rate-
      driven, not equity), and REITs (dividend / 90% distribution
      mechanics).
    * ``etf`` hides ETNs (issuer credit risk — see TVIX/XIV 2018) and
      leveraged/inverse ETFs (path-dependent decay; reversion +
      momentum both misbehave).
    * ``fund`` hides CEFs (price reversion ≠ NAV-discount reversion —
      category error to model as price-mean reversion).
    * ``spac`` conflates three instruments: Class A SPAC shares
      ($10-floored money-market-like w/ redemption option), warrants
      (long-dated OTM call w/ binary payoff), and units (short-lived
      basket).

Expand-not-rename strategy: keep the existing values active so the
~30 downstream consumers don't all break in one migration. Add new
values + an ``instrument_subtype`` column for finer-grained
distinctions. Backfill via OpenFIGI ``securityType2`` (already wired —
``tpcore/openfigi/figi_adapter.py`` returns ``security_type``).

Mapping (OpenFIGI securityType2 → asset_class[, instrument_subtype]):

    Common Stock                  → stock      (no subtype)
    REIT                          → reit       (no subtype)
    ADR                           → adr        (sponsored / unsponsored
                                                via securityType)
    Preferred                     → preferred  (no subtype)
    ETP (ETF)                     → etf        (vanilla / leveraged /
                                                inverse via etf_*)
    ETN                           → etn        (no subtype)
    Closed-End Fund               → cef        (no subtype)
    Mutual Fund                   → fund       (no subtype)
    SPAC (no suffix)              → spac       (share)
    SPAC ending .U / -U / 'Unit'  → spac       (unit)
    SPAC ending .W / -W / 'Wt'    → spac       (warrant)

Backwards-compat: existing ``WHERE asset_class = 'stock'`` queries still
work but exclude REITs / ADRs / preferreds (correctly — operator-aware
queries can opt into the broader common-equity set via
``asset_class IN ('stock', 'adr', 'reit')`` if needed).
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260530_0100"
down_revision: str | None = "20260527_0400"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Single SoT for the new valid set. Keep this list in sync with
# ``tpcore/openfigi/taxonomy.py::VALID_ASSET_CLASSES``.
_VALID_ASSET_CLASSES = (
    "stock", "adr", "preferred", "reit",
    "etf", "etn", "cef", "fund",
    "spac",
)

_VALID_INSTRUMENT_SUBTYPES = (
    # SPAC sub-instruments — the 78-of-82 Friday churn case.
    "share", "unit", "warrant",
    # ETF sub-instruments (REVIEW: today etf_leverage / etf_inverse
    # already encode this on ``ticker_classifications``; the subtype
    # column lets us collapse to a single signal for engine filters).
    "vanilla", "leveraged", "inverse",
    # ADR depositary-receipt depth.
    "sponsored", "unsponsored",
)


def upgrade() -> None:
    # ─── 1. expand asset_class CHECK constraint ────────────────────
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT IF EXISTS ticker_classifications_asset_class_chk"
    )
    valid_list = ", ".join(f"'{v}'::text" for v in _VALID_ASSET_CLASSES)
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD CONSTRAINT ticker_classifications_asset_class_chk "
        f"CHECK (asset_class = ANY (ARRAY[{valid_list}]))"
    )

    # ─── 2. expand etf_fields CHECK constraint ─────────────────────
    # The existing rule was: etf_inverse / etf_leverage / etf_category
    # are NULL when asset_class IN ('stock', 'spac'); otherwise required.
    # New rule: only ETF + ETN may carry those fields; everything else
    # (incl. the new finer classes adr/preferred/reit/cef/fund) MUST
    # have them NULL.
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT IF EXISTS ticker_classifications_etf_fields_chk"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD CONSTRAINT ticker_classifications_etf_fields_chk "
        "CHECK ("
        "  (asset_class IN ('etf', 'etn')) "
        "  OR (etf_inverse IS NULL AND etf_leverage IS NULL "
        "      AND etf_category IS NULL)"
        ")"
    )

    # ─── 3. add instrument_subtype column ──────────────────────────
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD COLUMN IF NOT EXISTS instrument_subtype text"
    )
    subtype_list = ", ".join(f"'{v}'::text" for v in _VALID_INSTRUMENT_SUBTYPES)
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD CONSTRAINT ticker_classifications_instrument_subtype_chk "
        f"CHECK (instrument_subtype IS NULL OR "
        f"instrument_subtype = ANY (ARRAY[{subtype_list}]))"
    )

    # ─── 4. index for per-class universe filtering ─────────────────
    # Engines + validators will frequently filter by
    # ``WHERE asset_class IN ('stock', 'adr', 'reit')`` — index helps.
    op.execute(
        "CREATE INDEX IF NOT EXISTS "
        "ix_ticker_classifications_asset_class_subtype "
        "ON platform.ticker_classifications (asset_class, instrument_subtype) "
        "WHERE asset_class IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS "
        "platform.ix_ticker_classifications_asset_class_subtype"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT IF EXISTS "
        "ticker_classifications_instrument_subtype_chk"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP COLUMN IF EXISTS instrument_subtype"
    )
    # Restore old CHECK constraints — but BLOCK if non-original
    # values are present (which would be data loss).
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT IF EXISTS ticker_classifications_etf_fields_chk"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD CONSTRAINT ticker_classifications_etf_fields_chk CHECK ("
        "  (asset_class IN ('stock', 'spac') AND etf_inverse IS NULL "
        "   AND etf_leverage IS NULL AND etf_category IS NULL) "
        "  OR (asset_class IN ('etf', 'fund'))"
        ")"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "DROP CONSTRAINT IF EXISTS ticker_classifications_asset_class_chk"
    )
    op.execute(
        "ALTER TABLE platform.ticker_classifications "
        "ADD CONSTRAINT ticker_classifications_asset_class_chk CHECK ("
        "  asset_class = ANY (ARRAY['stock'::text, 'etf'::text, "
        "                           'fund'::text, 'spac'::text])"
        ")"
    )
