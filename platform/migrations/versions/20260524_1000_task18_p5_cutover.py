"""Task #18 P5 — cutover: rename live tables -> *_legacy, promote _v views -> canonical names.

Per spec docs/superpowers/specs/2026-05-23-task-18-macro-data-consolidation.md §5.

After this migration:
  - platform.macro_indicators_legacy (was platform.macro_indicators)
    is a FROZEN snapshot — no further writes (producers updated in the
    same commit to call upsert_macro_data_bitemporal exclusively).
  - platform.macro_indicators is now the SHIM VIEW (was macro_indicators_v)
    reading current rows from platform.macro_data WHERE source='fred'.
  - Same pattern for aaii_sentiment + fear_greed.

Atomicity: alembic wraps the upgrade() in a single transaction by default;
all 6 RENAMEs commit together or roll back together. Consumers querying
the canonical names see no gap.

Consumer impact: zero (views expose the original column shapes —
macro_indicators(indicator, date, value, recorded_at), aaii_sentiment
(date, bullish_pct, bearish_pct, neutral_pct, recorded_at), fear_greed
(date, score, label, direction, score_5d_ago, 4 components, recorded_at)).
Engines + lab + indicators + stelib still read the canonical names.

Producer impact: the 3 macro handlers' legacy INSERT block is dropped
in the same commit (the legacy table is no longer writable through the
canonical name; the view that took its name has no INSTEAD OF trigger).

Soak window: parity tests stayed 4/4 green across the simulated cycle
that found and fixed the float-precision bug AND the SCD-2 duplicate-
current invariant. Operator authorised "cut over if green" 2026-05-24.

Revision ID: 20260524_1000
Revises: 20260524_0900
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_1000"
down_revision: str | None = "20260524_0900"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


_RENAMES: tuple[tuple[str, str], ...] = (
    ("macro_indicators",  "macro_indicators_v"),
    ("aaii_sentiment",    "aaii_sentiment_v"),
    ("fear_greed",        "fear_greed_v"),
)


def upgrade() -> None:
    for table_name, view_name in _RENAMES:
        # 1. Rename live table out of the way: live -> live_legacy
        op.execute(
            f"ALTER TABLE platform.{table_name} RENAME TO {table_name}_legacy"
        )
        # 2. Promote the shim view to take the original table's name
        op.execute(
            f"ALTER VIEW platform.{view_name} RENAME TO {table_name}"
        )


def downgrade() -> None:
    # Reverse the rename: canonical-name -> _v, _legacy -> canonical-name.
    # Producers must be reverted to the pre-P5 double-write shape before
    # this downgrade runs, otherwise the legacy table will silently miss
    # observations that landed in macro_data during the cutover window.
    for table_name, view_name in _RENAMES:
        op.execute(
            f"ALTER VIEW platform.{table_name} RENAME TO {view_name}"
        )
        op.execute(
            f"ALTER TABLE platform.{table_name}_legacy RENAME TO {table_name}"
        )
