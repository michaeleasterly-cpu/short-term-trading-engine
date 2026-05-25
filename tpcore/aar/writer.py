"""Idempotent writer for ``AfterActionReport`` rows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from tpcore.identity.dispatcher import IdentityDispatcher
from tpcore.lab.context import assert_not_in_lab

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

    Wave-4 E4 (2026-05-22) — write_aar now has a deferred-queue
    self-heal: an exception out of the underlying ``aar_events`` INSERT
    is caught, the AAR is enqueued to ``platform.aar_deferred`` via
    :class:`tpcore.aar.deferred.DeferredAARWriter`, and ``write_aar``
    returns ``False`` instead of re-raising. The engine cycle therefore
    continues even if the AAR-write substrate is transiently broken.
    A later replay (``replay_deferred_aars`` / ``ops.py --stage
    aar_replay``) lands the deferred row in ``aar_events``.
    """

    def __init__(
        self,
        db_pool: asyncpg.Pool | None = None,
        *,
        self_heal: bool = True,
    ) -> None:
        assert_not_in_lab()
        self._pool = db_pool
        # ``self_heal=False`` retains the pre-Wave-4 raise-on-failure
        # contract for the ONE call site that wants it — the replay
        # path itself (``replay_deferred_aars``) must NOT re-defer a
        # row whose write still fails: re-deferring would create an
        # infinite loop where every replay attempt enqueues a new
        # deferred row. The replay catches the exception itself and
        # leaves the original ``aar_deferred`` row pending.
        self._self_heal = self_heal
        # Dispatcher resolves the durable classification_id at write
        # time so aar_events.classification_id is populated alongside
        # the human-readable ticker. PR #331 added the column to the
        # table; this writer started populating it. None pool ⇒ no
        # dispatcher needed (DB-less environments).
        self._dispatcher = IdentityDispatcher(db_pool) if db_pool is not None else None

    @property
    def pool(self) -> asyncpg.Pool | None:
        """The asyncpg pool the writer was constructed with, or ``None``
        when wired in a DB-less environment. Exposed publicly so
        consumers that need the same pool (e.g. the order manager
        writing to ``platform.open_orders``) don't have to reach into
        ``_pool``. Added 2026-05-14 alongside ``RiskGovernor.state_for``.
        """
        return self._pool

    async def write_aar(self, aar: AfterActionReport) -> bool:
        """Insert ``aar`` if absent. Returns ``True`` iff a new row was written.

        On substrate exception (pool acquire failure, INSERT error, etc.)
        and ``self_heal=True``, the AAR is queued to
        ``platform.aar_deferred`` via :class:`tpcore.aar.deferred.
        DeferredAARWriter` and the method returns ``False``. The engine
        cycle therefore continues without losing the AAR.
        """
        if self._pool is None:
            return False

        # Resolve the durable classification_id at write time. None is
        # acceptable — for synthetic AARs whose ticker has no row in
        # ticker_history (rare; happens on first-issue dates), the
        # column stays NULL and the FK (nullable) permits it. The
        # human-readable ticker column is unchanged.
        cid: str | None = None
        if self._dispatcher is not None:
            cid = await self._dispatcher.ticker_to_classification_id(aar.ticker)

        sql = """
            INSERT INTO platform.aar_events
              (engine, trade_id, ticker, classification_id, aar_data, recorded_at)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6)
            ON CONFLICT (engine, trade_id) DO NOTHING
            RETURNING 1
        """
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    sql,
                    aar.engine,
                    aar.trade_id,
                    aar.ticker,
                    cid,
                    aar.model_dump_json(),
                    datetime.now(UTC),
                )
        except Exception as exc:  # noqa: BLE001 — Wave-4 E4 defer self-heal
            if not self._self_heal:
                raise
            # Local import: tpcore.aar.deferred imports AfterActionReport
            # from tpcore.aar.models, which is the same module we live
            # in. A top-level import would create a cycle on tpcore.aar
            # init (writer.py is imported by tpcore/aar/__init__.py).
            from tpcore.aar.deferred import DeferredAARWriter

            logger.warning(
                "tpcore.aar.write_failed_deferring",
                engine=aar.engine,
                trade_id=aar.trade_id,
                ticker=aar.ticker,
                error=f"{type(exc).__name__}: {exc}",
            )
            await DeferredAARWriter(self._pool).defer(aar, exc)
            return False
        wrote = row is not None
        logger.debug(
            "tpcore.aar.write",
            engine=aar.engine,
            trade_id=aar.trade_id,
            wrote=wrote,
        )
        return wrote


__all__ = ["AARWriter"]
