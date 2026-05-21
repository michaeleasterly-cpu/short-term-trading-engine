"""Postgres-backed ``RiskStateStore`` for cross-process / cross-cron-run state.

Mirrors the in-memory store interface and persists every mutation to the
``platform.risk_state`` table so daily/weekly P&L caps, the open-position
counter, and the kill switch survive process restarts (Railway recreates
the cron container on every fire).

Connection lifecycle follows the standard pool-acquire pattern: each method
borrows a connection, runs one statement, and releases. No long-lived
connection is held across method calls — this matters for the cron worker,
which must exit cleanly without leaking pool slots.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from .governor import RiskState, RiskStateStore

logger = structlog.get_logger(__name__)

_SELECT_COLUMNS = (
    "engine, engine_equity, daily_pnl, weekly_pnl, open_positions, "
    "daily_reset_at, weekly_reset_at, kill_switch_active, kill_switch_reason, updated_at"
)


def _row_to_state(row) -> RiskState:
    """Materialize a Postgres row into a ``RiskState`` Pydantic model."""
    return RiskState(
        engine=row["engine"],
        engine_equity=Decimal(str(row["engine_equity"])),
        daily_pnl=Decimal(str(row["daily_pnl"])),
        weekly_pnl=Decimal(str(row["weekly_pnl"])),
        open_positions=int(row["open_positions"]),
        daily_reset_at=row["daily_reset_at"],
        weekly_reset_at=row["weekly_reset_at"],
        kill_switch_active=bool(row["kill_switch_active"]),
        kill_switch_reason=row["kill_switch_reason"],
        updated_at=row["updated_at"] or datetime.now(UTC),
    )


class PostgresRiskStateStore(RiskStateStore):
    """Persists ``RiskState`` to ``platform.risk_state``.

    Args:
        pool: an ``Any`` (or anything with the same async-context
            ``acquire()`` semantics). The store does not own the pool's
            lifecycle — the caller (the scheduler) closes it on exit.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def get(self, engine_id: str) -> RiskState | None:
        sql = f"SELECT {_SELECT_COLUMNS} FROM platform.risk_state WHERE engine = $1"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(sql, engine_id)
        return _row_to_state(row) if row is not None else None

    async def put(self, state: RiskState) -> None:
        """Upsert ``state`` into ``platform.risk_state`` keyed by engine."""
        sql = """
            INSERT INTO platform.risk_state (
                engine, engine_equity, daily_pnl, weekly_pnl, open_positions,
                daily_reset_at, weekly_reset_at, kill_switch_active, kill_switch_reason,
                updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
            ON CONFLICT (engine) DO UPDATE SET
                engine_equity = EXCLUDED.engine_equity,
                daily_pnl = EXCLUDED.daily_pnl,
                weekly_pnl = EXCLUDED.weekly_pnl,
                open_positions = EXCLUDED.open_positions,
                daily_reset_at = EXCLUDED.daily_reset_at,
                weekly_reset_at = EXCLUDED.weekly_reset_at,
                kill_switch_active = EXCLUDED.kill_switch_active,
                kill_switch_reason = EXCLUDED.kill_switch_reason,
                updated_at = now()
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                sql,
                state.engine,
                state.engine_equity,
                state.daily_pnl,
                state.weekly_pnl,
                state.open_positions,
                state.daily_reset_at,
                state.weekly_reset_at,
                state.kill_switch_active,
                state.kill_switch_reason,
            )

    async def record_close(
        self,
        engine: str,
        trade_id: str | None,
        realized_pnl: Decimal,
    ) -> bool:
        """Idempotent close-decrement arbitrated by ``risk_close_ledger``.

        ONE transaction: ``INSERT … ON CONFLICT DO NOTHING`` keyed by
        ``(engine, trade_id)``; iff the insert won (rowcount == 1) →
        ``open_positions = GREATEST(0, open_positions-1)`` and pnl applied
        once, return ``True``; else (conflict / already counted / race
        loser) → return ``False``, NO decrement, NO pnl change.

        ``trade_id is None`` → WARN, return ``False``, NO decrement (a
        missing id is never guessed — over-count is safe, never fail
        open). See #251 spec §2b. Mirrors
        :meth:`InMemoryRiskStateStore.record_close` exactly.
        """
        if trade_id is None:
            logger.warning(
                "tpcore.risk.record_close_null_trade_id",
                engine=engine,
                detail="trade_id is None — skipping the decrement (over-count "
                       "is safe; never guess a close id → never fail open)",
            )
            return False
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                status = await conn.execute(
                    "INSERT INTO platform.risk_close_ledger (engine, trade_id) "
                    "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    engine,
                    trade_id,
                )
                # DB driver execute() returns e.g. "INSERT 0 1" (1 row) or
                # "INSERT 0 0" (ON CONFLICT skipped).
                try:
                    inserted = int(status.split()[-1]) == 1
                except (ValueError, IndexError):  # never fail open on a parse miss
                    inserted = False
                if not inserted:
                    return False
                await conn.execute(
                    "UPDATE platform.risk_state "
                    "SET open_positions = GREATEST(0, open_positions - 1), "
                    "    daily_pnl = daily_pnl + $2, "
                    "    weekly_pnl = weekly_pnl + $2, "
                    "    updated_at = now() "
                    "WHERE engine = $1",
                    engine,
                    realized_pnl,
                )
        logger.info(
            "tpcore.risk.close_recorded",
            engine=engine,
            trade_id=trade_id,
            realized_pnl=str(realized_pnl),
        )
        return True

    async def list_all(self) -> list[RiskState]:
        sql = f"SELECT {_SELECT_COLUMNS} FROM platform.risk_state ORDER BY engine"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql)
        return [_row_to_state(r) for r in rows]

    async def set_kill_switch_all(self, *, active: bool, reason: str | None = None) -> None:
        sql = """
            UPDATE platform.risk_state
            SET kill_switch_active = $1,
                kill_switch_reason = $2,
                updated_at = now()
        """
        async with self._pool.acquire() as conn:
            await conn.execute(sql, active, reason if active else None)
        logger.warning(
            "tpcore.risk.kill_switch_all", active=active, reason=reason if active else None
        )


__all__ = ["PostgresRiskStateStore"]
