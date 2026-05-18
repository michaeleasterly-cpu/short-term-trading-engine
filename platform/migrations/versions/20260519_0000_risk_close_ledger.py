"""risk_close_ledger — idempotent arbiter for the close-decrement (#251 B1)

A single position close used to decrement ``platform.risk_state.
open_positions`` via TWO uncoordinated paths (the scheduler rebalance-sell
loop and the trade-monitor stream), with no shared chokepoint and a
last-writer-wins ``put()`` — a monotonic under-drift that eventually
fails the never-fail-open RiskGovernor open.

This table is the atomic arbiter: ``record_close`` does, in ONE
transaction, ``INSERT … ON CONFLICT DO NOTHING`` keyed by
``(engine, trade_id)``; only the insert-winner applies the ``-1`` /
realized-pnl. The unique PK guarantees a real close decrements AT MOST
once across every interleaving — never fail open.

Bounded by a 14-day prune (a settled ``trade_id`` is never re-closed),
wired into the existing ``ops.py`` maintenance cadence — not a daemon.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260519_0000"
down_revision: str | None = "20260517_0900"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.risk_close_ledger (
            engine      text        NOT NULL,
            trade_id    text        NOT NULL,
            recorded_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (engine, trade_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform.risk_close_ledger")
