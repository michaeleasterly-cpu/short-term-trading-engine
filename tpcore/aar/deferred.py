"""Deferred-AAR queue — Wave-4 row E4 of the deterministic self-heal expansion.

Reference: ``docs/superpowers/specs/2026-05-21-deterministic-self-heal-
coverage-expansion-design.md`` row E4 + §4 answer #4.

Design summary:

* :class:`tpcore.aar.writer.AARWriter` is the canonical write path. Before
  Wave-4 it had ONE failure mode the engine cycle couldn't recover from:
  the underlying ``INSERT INTO platform.aar_events`` raised (transient
  pool exhaustion, ``asyncpg.exceptions.DataError`` on a JSON column,
  validation error, etc.). The cycle would crash mid-AAR-emit and the
  AAR record was lost.
* Wave-4 E4 inserts a deferred-queue substrate (``platform.aar_deferred``)
  + a :class:`DeferredAARWriter` that catches the original write
  exception, enqueues the AAR to the substrate, emits the
  ``AAR_DEFERRED`` event, and lets the cycle continue.
* A replay step (:func:`replay_deferred_aars`) drains the queue: pull
  pending rows oldest-first, attempt the same ``aar_events`` insert via
  :class:`tpcore.aar.writer.AARWriter`, and on success mark the deferred
  row's ``replayed_at = now()``. Failures stay queued for the next run.

The replay can run on every engine cycle (after the AAR write loop)
OR explicitly via ``python scripts/ops.py --stage aar_replay``. Both
paths are idempotent against duplicate AARs because the canonical
``aar_events`` table enforces ``(engine, trade_id)`` uniqueness ON
CONFLICT DO NOTHING — re-running the replay with a row that succeeded
is a no-op against ``aar_events`` and we still mark the defer row
replayed_at so it stops showing up in the pending query.

Event: ``AAR_DEFERRED`` is emitted to ``platform.application_log`` with
``severity=WARNING``; the operator sees one per defer (NOT per AAR
production) so a single broken cycle produces a manageable log volume.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from tpcore.aar.models import AfterActionReport

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


# Public event name emitted to ``platform.application_log`` when the
# canonical AAR write raises and the record is queued to
# ``platform.aar_deferred`` instead. Distinct from ``AAR_REPLAYED`` (the
# replay-success structured-log breadcrumb, info-only — no
# application_log row by design; the replay is the recovery path, not a
# fresh escalation).
AAR_DEFERRED_EVENT: str = "AAR_DEFERRED"


# Truncation cap for the ``defer_reason`` column so a runaway
# exception ``str()`` (e.g. a 64KB asyncpg error with a copy of the
# bound parameters) can't bloat the substrate. 480 chars is long
# enough to keep the exception class + the most actionable prefix of
# the message, short enough that a million deferred rows weighs in
# under ~500MB. Same scale as the ``ORDER_ESCALATED`` event payload
# truncation in ``tpcore.order_management.transient_retry``.
_DEFER_REASON_MAX_CHARS: int = 480


_INSERT_DEFER_SQL = """
    INSERT INTO platform.aar_deferred
        (engine, trade_id, ticker, aar_data, defer_reason)
    VALUES ($1, $2, $3, $4::jsonb, $5)
    RETURNING id
"""


_INSERT_APP_LOG_SQL = """
    INSERT INTO platform.application_log
        (engine, run_id, event_type, severity, message, data)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
"""


_SELECT_PENDING_SQL = """
    SELECT id, engine, trade_id, ticker, aar_data, defer_reason,
           recorded_at
    FROM platform.aar_deferred
    WHERE replayed_at IS NULL
    ORDER BY recorded_at ASC
    LIMIT $1
"""


_MARK_REPLAYED_SQL = """
    UPDATE platform.aar_deferred
    SET replayed_at = now()
    WHERE id = $1
"""


def _truncate_reason(exc: BaseException) -> str:
    """Render an exception as ``ClassName: message`` capped at
    ``_DEFER_REASON_MAX_CHARS`` so the column never explodes.

    The class name is preserved in full (operator scans by it); the
    message is right-truncated and an ellipsis appended so it's visible
    that we cut something.
    """
    body = f"{type(exc).__name__}: {exc}"
    if len(body) <= _DEFER_REASON_MAX_CHARS:
        return body
    return body[: _DEFER_REASON_MAX_CHARS - 3] + "..."


async def _emit_aar_deferred_event(
    pool: asyncpg.Pool | None,
    *,
    engine: str,
    trade_id: str,
    ticker: str,
    defer_id: uuid.UUID | str,
    defer_reason: str,
) -> None:
    """Crash-isolated emit of an ``AAR_DEFERRED`` row to application_log.

    Mirrors the shape of ``tpcore.order_management.transient_retry.
    _emit_application_log`` — pool may be ``None`` (tests/no-DB
    environments) and any emit exception is swallowed so the
    self-heal path itself NEVER raises out of the defer attempt.
    """
    if pool is None:
        return
    payload = {
        "schema": 1,
        "engine": engine,
        "trade_id": trade_id,
        "ticker": ticker,
        "defer_id": str(defer_id),
        "defer_reason": defer_reason,
    }
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                _INSERT_APP_LOG_SQL,
                engine,
                uuid.uuid4(),
                AAR_DEFERRED_EVENT,
                "WARNING",
                (
                    f"{engine} AAR deferred for trade_id={trade_id}: "
                    f"{defer_reason[:160]}"
                ),
                json.dumps(payload, default=str),
            )
    except Exception as exc:  # noqa: BLE001 — observability is best-effort
        logger.warning(
            "tpcore.aar.deferred.emit_failed",
            engine=engine,
            trade_id=trade_id,
            error=str(exc),
        )


class DeferredAARWriter:
    """Enqueues an :class:`AfterActionReport` to ``platform.aar_deferred``.

    Called by :class:`tpcore.aar.writer.AARWriter` when the canonical
    ``aar_events`` insert raises. A ``None`` pool is treated as "DB
    not wired in this environment" — the defer is logged via structlog
    and skipped (matches the existing :class:`AARWriter` no-pool
    contract; tests don't need a fixture for this seam).
    """

    def __init__(self, db_pool: asyncpg.Pool | None = None) -> None:
        self._pool = db_pool

    @property
    def pool(self) -> asyncpg.Pool | None:
        return self._pool

    async def defer(
        self,
        aar: AfterActionReport,
        original_exc: BaseException,
    ) -> str | None:
        """Persist ``aar`` to the deferred queue and emit ``AAR_DEFERRED``.

        Returns the deferred-row id (as ``str``) on success, or ``None``
        when the pool is unwired. The original exception is captured in
        the ``defer_reason`` column AND included in the emitted event so
        the operator can triage by exception class.

        Best-effort: a failure of the DEFER insert itself is logged via
        structlog and returned as ``None`` — never raises, because the
        caller (AAR writer's exception path) must always return to the
        engine cycle.
        """
        defer_reason = _truncate_reason(original_exc)
        if self._pool is None:
            logger.warning(
                "tpcore.aar.deferred.no_pool",
                engine=aar.engine,
                trade_id=aar.trade_id,
                ticker=aar.ticker,
                defer_reason=defer_reason,
                detail="no asyncpg pool wired — AAR defer skipped",
            )
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    _INSERT_DEFER_SQL,
                    aar.engine,
                    aar.trade_id,
                    aar.ticker,
                    aar.model_dump_json(),
                    defer_reason,
                )
        except Exception as defer_exc:  # noqa: BLE001 — self-heal never raises
            # The defer itself failed. Log loud (operator must see the
            # double failure) and return None — the original AAR is now
            # lost-in-process, but the engine cycle continues. The
            # operator's signal is the structlog ERROR, not a raised
            # exception that would crash the cycle.
            logger.error(
                "tpcore.aar.deferred.defer_failed",
                engine=aar.engine,
                trade_id=aar.trade_id,
                ticker=aar.ticker,
                original=defer_reason,
                defer_error=f"{type(defer_exc).__name__}: {defer_exc}",
            )
            return None
        defer_id = row["id"]
        await _emit_aar_deferred_event(
            self._pool,
            engine=aar.engine,
            trade_id=aar.trade_id,
            ticker=aar.ticker,
            defer_id=defer_id,
            defer_reason=defer_reason,
        )
        logger.warning(
            "tpcore.aar.deferred.queued",
            engine=aar.engine,
            trade_id=aar.trade_id,
            ticker=aar.ticker,
            defer_id=str(defer_id),
            defer_reason=defer_reason,
        )
        return str(defer_id)


async def replay_deferred_aars(
    pool: asyncpg.Pool | None,
    *,
    limit: int = 100,
) -> dict[str, int]:
    """Drain pending rows from ``platform.aar_deferred`` into ``aar_events``.

    Args:
        pool: asyncpg pool. ``None`` returns the empty zero-counts dict
            so tests without a pool stay green.
        limit: maximum rows to attempt per call (oldest-first). Keeps a
            single replay bounded under the ``cmd_update`` stage
            timeout; a hundred per call clears a typical day's
            transient defer surface and the next run picks up the
            rest. Same pattern as ``risk_close_ledger_prune``.

    Returns:
        Counts dict ``{"pending": N, "replayed": K, "still_failing": F}``
        — ``pending`` is the inspected count, ``replayed`` is how many
        landed in ``aar_events`` AND were marked replayed_at, and
        ``still_failing`` is the count whose ``aar_events`` insert
        raised again. The counts dict is the operator's signal.
    """
    counts = {"pending": 0, "replayed": 0, "still_failing": 0}
    if pool is None:
        return counts

    # Import locally to avoid a circular dependency with
    # tpcore.aar.writer (which imports this module's DeferredAARWriter
    # at module top).
    from tpcore.aar.writer import AARWriter

    writer = AARWriter(pool)
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SELECT_PENDING_SQL, limit)
    counts["pending"] = len(rows)
    for row in rows:
        try:
            aar = AfterActionReport.model_validate_json(row["aar_data"])
        except Exception as exc:  # noqa: BLE001 — corrupted defer never crashes the replay
            logger.error(
                "tpcore.aar.deferred.rehydrate_failed",
                defer_id=str(row["id"]),
                engine=row["engine"],
                trade_id=row["trade_id"],
                error=f"{type(exc).__name__}: {exc}",
            )
            counts["still_failing"] += 1
            continue
        try:
            await writer.write_aar(aar)
        except Exception as exc:  # noqa: BLE001 — substrate still down: skip + retry next run
            logger.warning(
                "tpcore.aar.deferred.replay_retry",
                defer_id=str(row["id"]),
                engine=aar.engine,
                trade_id=aar.trade_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            counts["still_failing"] += 1
            continue
        # write_aar returns False on (engine, trade_id) ON CONFLICT —
        # the idempotency contract means a re-replay of a row whose
        # original AAR somehow already landed is still a SUCCESS for
        # the queue (mark replayed_at; row drops out of the pending
        # query). We deliberately do NOT branch on the bool.
        async with pool.acquire() as conn:
            await conn.execute(_MARK_REPLAYED_SQL, row["id"])
        counts["replayed"] += 1
        logger.info(
            "tpcore.aar.deferred.replayed",
            defer_id=str(row["id"]),
            engine=aar.engine,
            trade_id=aar.trade_id,
            ticker=aar.ticker,
            recorded_at=row["recorded_at"].isoformat()
            if row["recorded_at"] is not None
            else None,
            replayed_at=datetime.now(UTC).isoformat(),
        )
    return counts


__all__ = [
    "AAR_DEFERRED_EVENT",
    "DeferredAARWriter",
    "replay_deferred_aars",
]
