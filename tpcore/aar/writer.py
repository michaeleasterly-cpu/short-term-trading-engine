"""Idempotent writer for ``AfterActionReport`` rows."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from .models import AfterActionReport

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


class AARWriter:
    """Persists ``AfterActionReport`` rows to ``platform.aar_events``.

    Idempotency is enforced by the ``(engine, trade_id)`` unique constraint
    plus ``ON CONFLICT DO NOTHING`` (D-137 Pattern A) — re-writing the same
    AAR is a no-op, so the order manager is free to call this more than
    once if reconciliation runs see the same fill on consecutive sessions.

    A ``None`` pool is treated as "DB not wired in this environment" — the
    writer skips the insert and returns ``False``. The order manager has
    already emitted the AAR via structlog by the time it calls us.
    """

    def __init__(self, db_pool: "asyncpg.Pool | None" = None) -> None:
        self._pool = db_pool

    async def write_aar(self, aar: AfterActionReport) -> bool:
        """Insert ``aar`` if absent. Returns ``True`` iff a new row was written."""
        if self._pool is None:
            return False

        sql = """
            INSERT INTO platform.aar_events (engine, trade_id, ticker, aar_data, recorded_at)
            VALUES ($1, $2, $3, $4::jsonb, $5)
            ON CONFLICT (engine, trade_id) DO NOTHING
            RETURNING 1
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                sql,
                aar.engine,
                aar.trade_id,
                aar.ticker,
                aar.model_dump_json(),
                datetime.now(UTC),
            )
        wrote = row is not None
        logger.debug(
            "tpcore.aar.write",
            engine=aar.engine,
            trade_id=aar.trade_id,
            wrote=wrote,
        )
        return wrote


__all__ = ["AARWriter"]
