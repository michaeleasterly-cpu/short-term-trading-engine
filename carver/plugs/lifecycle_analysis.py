"""Carver — Plug 2: Lifecycle analysis (rebalance phase + speed-limit counter).

Portfolio-allocation engines have no per-trade lifecycle in the per-trade
engines' sense. This plug owns:

1. ``current_phase(as_of)`` — returns ``Phase.REBALANCE`` on the
   ``MONTHLY_FIRST_TRADING_DAY`` cadence boundary, else ``Phase.HOLDING``.
   Pure helper for tests/dashboard. The dispatcher gates cadence; this is
   informational only.
2. ``record_trade_flip(pool, ticker, as_of)`` — appends a CARVER_FLIP
   event to ``platform.application_log`` for the rolling-12-month
   speed-limit counter.
3. ``flips_in_window(pool, ticker, as_of, days=365)`` — read-side
   query of (2). Used by the execution-risk plug to suppress a 13th flip
   in a year.

Persistence: the rolling counter lives in ``platform.application_log``
under ``event_type='CARVER_FLIP'`` keyed by ticker; no new schema.

See ``docs/superpowers/specs/2026-05-20-carver-design.md`` Section 4.2.
"""
from __future__ import annotations

import json
from datetime import date as date_t
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from carver.models import Phase
from tpcore.calendar import first_session_of_month
from tpcore.interfaces.engine_plug import BaseEnginePlug

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


class CarverLifecycleAnalysis(BaseEnginePlug):
    """Plug 2 of Carver — rebalance phase + per-instrument speed-limit counter."""

    engine_name = "carver"

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "lifecycle_analysis",
            "ok": True,
            "details": {"cadence": "monthly_first_trading_day"},
        }

    @staticmethod
    def current_phase(as_of: date_t) -> Phase:
        """Pure helper: REBALANCE on first trading day of month, else HOLDING."""
        first = first_session_of_month(as_of.year, as_of.month)
        return Phase.REBALANCE if first == as_of else Phase.HOLDING

    @staticmethod
    async def record_trade_flip(
        pool: asyncpg.Pool, ticker: str, as_of: date_t,
    ) -> None:
        """Append a CARVER_FLIP event to platform.application_log.

        Idempotent at the SQL level via the (engine, event_type, data, recorded_at)
        application_log shape. The execution-risk plug calls this once per
        direction-flip per rebalance day."""
        sql = """
            INSERT INTO platform.application_log
                (engine, event_type, severity, message, data, recorded_at)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6)
        """
        payload = json.dumps({"ticker": ticker, "as_of": as_of.isoformat()})
        async with pool.acquire() as conn:
            await conn.execute(
                sql,
                "carver",
                "CARVER_FLIP",
                "INFO",
                f"carver direction flip on {ticker} at {as_of.isoformat()}",
                payload,
                datetime.combine(as_of, datetime.min.time()),
            )
        logger.info("carver.lifecycle.flip_recorded", ticker=ticker, as_of=as_of.isoformat())

    @staticmethod
    async def flips_in_window(
        pool: asyncpg.Pool,
        ticker: str,
        as_of: date_t,
        days: int = 365,
    ) -> int:
        """Count CARVER_FLIP events for ``ticker`` in the trailing ``days`` window."""
        start = as_of - timedelta(days=days)
        sql = """
            SELECT COUNT(*) AS n
            FROM platform.application_log
            WHERE engine = 'carver'
              AND event_type = 'CARVER_FLIP'
              AND recorded_at >= $1
              AND recorded_at <= $2
              AND data->>'ticker' = $3
        """
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                sql,
                datetime.combine(start, datetime.min.time()),
                datetime.combine(as_of, datetime.max.time()),
                ticker,
            )
        return int(row["n"]) if row and row["n"] is not None else 0


__all__ = ["CarverLifecycleAnalysis"]
