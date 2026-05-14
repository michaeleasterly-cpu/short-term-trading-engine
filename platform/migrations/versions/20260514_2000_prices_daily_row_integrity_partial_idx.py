"""prices_daily — partial index over the row_integrity violation predicate

The validation suite's ``row_integrity`` check (tpcore/quality/validation/
checks/row_integrity.py) scans the entire 20M-row ``prices_daily`` table
for any row violating physical-truth predicates (close ≤ 0, OHLC
inconsistent, NULL OHLCV, future dates). Pre-gates this routinely found
hundreds of rows; post-gates (commit 9418e61, 2026-05-14) the count is 0
because the ingest writer rejects them at the source.

Problem: even a 0-hit scan over 20M rows takes ~140s and trips Supabase's
statement timeout, so the validation check times out instead of passing.

Fix: a partial B-tree index whose WHERE clause IS the violation predicate.
PostgreSQL keeps only the matching rows in the index — normally an empty
index. The planner uses the partial index for the predicate scan, so
``SELECT COUNT(*) WHERE <predicate>`` returns near-instantly (∝ violation
count, not table size). When ingest gates fail, this also surfaces
violations faster.

The predicate matches the validation check exactly; if the check predicate
changes (e.g., a new physical-truth rule is added), this index must be
rebuilt to match. The check file documents the index dependency in its
header comment.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260514_2000"
down_revision: str | None = "20260514_1800"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF NOT EXISTS so re-running this migration after manual application
    # is a no-op (the data-validation gate auto-heals on the next post-close
    # run; this migration is the persistent fix).
    # NOTE: ``date > CURRENT_DATE`` is excluded — Postgres rejects
    # non-IMMUTABLE expressions in partial-index predicates. Future-date
    # rejection is now enforced at the ingest writer (ingest_alpaca_bars
    # rejects rows with date > today) so this index covers every
    # physical-truth gate that *can* still be violated.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_prices_daily_row_integrity_violations
        ON platform.prices_daily (date, ticker)
        WHERE
               close IS NULL OR close <= 0 OR close > 100000000
            OR open IS NULL OR high IS NULL OR low IS NULL
            OR high < GREATEST(open, close, low)
            OR low > LEAST(open, close, high)
            OR volume IS NULL OR volume < 0
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS platform.idx_prices_daily_row_integrity_violations"
    )
