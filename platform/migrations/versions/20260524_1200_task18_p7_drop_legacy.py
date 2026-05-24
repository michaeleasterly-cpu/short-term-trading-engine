"""Task #18 P7 — drop the 3 _legacy tables + the 3 legacy-shape shim VIEWs.

Per operator directive 2026-05-24 ("migrate completely to the new table
and remove all the legacy tables for macro... the data is the gateway").

This migration is the irreversible cutover. After it lands, the only
canonical macro storage is platform.macro_data; the shim views that
exposed the legacy column shapes (macro_indicators, aaii_sentiment,
fear_greed) are GONE, as are the *_legacy frozen tables.

What this breaks (intentionally — operator scope to fix):
  - sentinel/{backtest,models,plugs/setup_detection}.py — engine reads
  - reversion/{regime_filter,backtest,plugs/setup_detection}.py — engine reads
  - vector/plugs/{setup_detection,execution_risk,lifecycle_analysis}.py
  - catalyst/tests/test_lab_macro_expansion_*.py
  - tpcore/lab/llm_finder/snapshot.py
The operator will refactor these to query platform.macro_data directly
(or via per-engine views the operator declares) when they resume engine
work.

What this does NOT break (already migrated in this commit):
  - All 4 validation checks read platform.macro_data
  - Both selfheal probes read platform.macro_data
  - All 4 macro producers (3 handlers + targeted_repull) write only macro_data
  - scripts/audit_data_pipeline.py + dump_baseline_archives.py +
    probe_reversion_partial_axis.py read macro_data
  - All test fixtures + tests updated

Pre-conditions (verified in the same commit):
  - Every (source, series_id) in platform.macro_data has a row in
    platform.series_catalog (operator-directed metadata catalog landed
    in migration 20260524_1100).
  - The repaired parity verification (4/4 green pre-drop) confirmed
    history-aware equivalence between *_legacy and macro_data.

Revision ID: 20260524_1200
Revises: 20260524_1100
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_1200"
down_revision: str | None = "20260524_1100"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


_LEGACY_TABLES: tuple[str, ...] = (
    "macro_indicators_legacy",
    "aaii_sentiment_legacy",
    "fear_greed_legacy",
)

_SHIM_VIEWS: tuple[str, ...] = (
    "macro_indicators",
    "aaii_sentiment",
    "fear_greed",
)


def upgrade() -> None:
    # Drop the shim VIEWs first (no dependents on macro_data — safe order).
    for view_name in _SHIM_VIEWS:
        op.execute(f"DROP VIEW IF EXISTS platform.{view_name}")

    # Drop the frozen _legacy tables (no FKs into them; no readers in the
    # post-cutover codebase).
    for table_name in _LEGACY_TABLES:
        op.execute(f"DROP TABLE IF EXISTS platform.{table_name}")


def downgrade() -> None:
    # IRREVERSIBLE: the _legacy tables' data is irrecoverable from the
    # migration alone (the original data lives in platform.macro_data via
    # the P2 backfill, but reconstructing the wide-shape legacy tables
    # would require a separate restore from the off-platform CSV archives
    # — out of scope for an alembic downgrade()).
    #
    # If you need the legacy shapes back, the *_v shim views are easy to
    # recreate from macro_data (their definitions are in migration
    # 20260524_0900). Restore them via SQL by hand; don't try to alembic
    # downgrade past this point.
    raise NotImplementedError(
        "Task #18 P7 is intentionally irreversible. The _legacy tables "
        "are frozen snapshots that were never re-populated post-cutover; "
        "alembic cannot reconstruct them. Re-create the shim views from "
        "platform.macro_data by hand if needed."
    )
