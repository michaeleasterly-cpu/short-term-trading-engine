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
from typing import TYPE_CHECKING

import structlog

from .governor import RiskState, RiskStateStore

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

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
        pool: an ``asyncpg.Pool`` (or anything with the same async-context
            ``acquire()`` semantics). The store does not own the pool's
            lifecycle — the caller (the scheduler) closes it on exit.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
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
