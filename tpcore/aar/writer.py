"""Idempotent writer for AfterActionReport rows."""
from __future__ import annotations

from datetime import UTC, datetime

from .models import AfterActionReport


class AARWriter:
    """Persists ``AfterActionReport`` to ``platform.aar_events``.

    Idempotency is enforced via the ``(engine, trade_id)`` unique constraint
    plus ``ON CONFLICT DO NOTHING``.
    """

    def __init__(self, db_pool) -> None:
        self._pool = db_pool

    async def write_aar(self, aar: AfterActionReport) -> bool:
        """Insert ``aar`` if absent. Returns True iff a new row was written.

        TODO: implement with asyncpg. Pseudocode::

            INSERT INTO platform.aar_events (
                engine, trade_id, ticker, aar_data, recorded_at
            ) VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (engine, trade_id) DO NOTHING
            RETURNING 1
        """
        _ = (aar, self._pool, datetime.now(UTC))  # silence unused warnings
        raise NotImplementedError
