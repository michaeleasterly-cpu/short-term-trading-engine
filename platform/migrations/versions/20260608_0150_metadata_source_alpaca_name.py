"""Allow ``metadata_source='alpaca_name'`` on ticker_classifications.

Spec: ``docs/superpowers/specs/2026-06-08-data-foundation-systemic-fix-
design.md`` §3.6 (SEC-authoritative asset_class; Alpaca-name-derived is the
honest fallback provenance). Plan Phase B/C.

WHY: the clean-slate staged spine
(``tpcore/identity/staging_spine_build.py:408``) records
``metadata_source = 'sec_submissions'`` when the asset_class is SEC-verified
and ``'alpaca_name'`` otherwise (the 10,728 rows whose asset_class is derived
from the Alpaca instrument name, not an SEC filing). The live CHECK
``ticker_classifications_metadata_source_chk`` predates this build and allows
only ``['sec_companyfacts','sec_submissions','manual','fmp_profile']`` — so the
clean spine swap would fail the constraint.

The fix is to RECOGNIZE the honest provenance value, NOT to remap it to a
misleading source (remapping ``alpaca_name -> fmp_profile`` would falsify where
the classification came from — a source-authority violation). This migration
extends the CHECK to include ``'alpaca_name'``; the downgrade restores the
prior four-value set.

This is a precondition migration for the destructive spine cut: it MUST land
before the staged spine is INSERTed. No data is touched (the live spine has
zero ``alpaca_name`` rows today; only ``NULL`` + ``sec_submissions`` are used).

Revision ID: 20260608_0150
Revises: 20260608_0100
Create Date: 2026-06-08
"""
from __future__ import annotations

from alembic import op

revision = "20260608_0150"
down_revision = "20260608_0100"
branch_labels = None
depends_on = None

_CHK = "ticker_classifications_metadata_source_chk"
_ALLOWED_NEW = (
    "sec_companyfacts", "sec_submissions", "manual", "fmp_profile", "alpaca_name",
)
_ALLOWED_OLD = ("sec_companyfacts", "sec_submissions", "manual", "fmp_profile")


def _chk_sql(values: tuple[str, ...]) -> str:
    arr = ", ".join(f"'{v}'::text" for v in values)
    return (
        f"ALTER TABLE platform.ticker_classifications "
        f"ADD CONSTRAINT {_CHK} CHECK ("
        f"metadata_source IS NULL OR metadata_source = ANY (ARRAY[{arr}]))"
    )


def upgrade() -> None:
    op.execute(f"ALTER TABLE platform.ticker_classifications DROP CONSTRAINT IF EXISTS {_CHK}")
    op.execute(_chk_sql(_ALLOWED_NEW))


def downgrade() -> None:
    op.execute(f"ALTER TABLE platform.ticker_classifications DROP CONSTRAINT IF EXISTS {_CHK}")
    op.execute(_chk_sql(_ALLOWED_OLD))
