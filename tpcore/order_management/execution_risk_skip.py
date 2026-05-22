"""Per-trade execution_risk skip — Wave-4 row E10 of the deterministic
self-heal expansion.

Reference: ``docs/superpowers/specs/2026-05-21-deterministic-self-heal-
coverage-expansion-design.md`` row E10.

Design summary:

* Each engine's ``execution_risk`` plug is called per candidate trade
  to construct the order payloads. Today, an exception out of that
  plug (sizing error, malformed assessment, division-by-zero, etc.)
  bubbles up the scheduler stack and aborts the WHOLE engine cycle —
  the remaining candidates that were going to fire get dropped silently.
* Wave-4 E10 inserts a per-trade boundary: the helper here
  (:func:`execute_with_risk_skip`) catches the exception, cancels any
  in-flight orders for the affected trade (via a caller-provided
  cleanup hook), emits the ``EXECUTION_RISK_ESCALATED`` event, and
  returns ``None`` instead of re-raising. The scheduler then SKIPS
  THE TRADE and moves on; the rest of the cycle continues.

The helper is engine-agnostic: each engine's scheduler opts in by
wrapping its execution_risk plug call. This is the same shape as the
order-submit transient retry in ``tpcore.order_management.
transient_retry.submit_with_transient_retry`` — a thin per-trade
guard around the existing plug call.

Event: ``EXECUTION_RISK_ESCALATED`` is emitted to ``platform.
application_log`` with ``severity=ERROR``; the operator sees exactly
one per failed trade (not one per cycle), so a single recurring
execution_risk defect creates a manageable log volume.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar

import structlog

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


# Public event name emitted to ``platform.application_log`` when an
# execution_risk plug call raises and the trade is skipped via the
# Wave-4 E10 boundary. Distinct from ``ORDER_ESCALATED`` (the
# post-submit transient-retry path) and ``ORDER_REJECTED`` (the
# RiskGovernor hard-reject path) — execution_risk runs UPSTREAM of
# order submit, so an escalation here means the trade never even
# entered the broker.
EXECUTION_RISK_ESCALATED_EVENT: str = "EXECUTION_RISK_ESCALATED"


_INSERT_SQL = """
    INSERT INTO platform.application_log
        (engine, run_id, event_type, severity, message, data)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
"""


async def _emit_application_log(
    pool: asyncpg.Pool | None,
    *,
    engine: str,
    event_type: str,
    severity: str,
    message: str,
    payload: dict[str, Any],
) -> None:
    """Crash-isolated emit (mirrors transient_retry._emit_application_log)."""
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                _INSERT_SQL,
                engine,
                uuid.uuid4(),
                event_type,
                severity,
                message,
                json.dumps(payload, default=str),
            )
    except Exception as exc:  # noqa: BLE001 — observability is best-effort
        logger.warning(
            "tpcore.order_management.execution_risk_skip.emit_failed",
            engine=engine,
            event_type=event_type,
            error=str(exc),
        )


_T = TypeVar("_T")


async def execute_with_risk_skip(
    decide: Callable[[], Awaitable[_T] | _T],
    *,
    pool: asyncpg.Pool | None,
    engine: str,
    ticker: str,
    cancel_in_flight: Callable[[], Awaitable[None]] | None = None,
    telemetry: dict[str, Any] | None = None,
) -> _T | None:
    """Run an execution_risk plug call with per-trade exception isolation.

    Args:
        decide: zero-arg callable that invokes the engine's
            ``execution_risk.decide(assessment, ...)`` plug. Can be
            sync or async — we ``await`` the result iff it's
            awaitable. Most engine plugs are sync today; sentinel /
            momentum may return a coroutine in the future.
        pool: asyncpg pool for the ``EXECUTION_RISK_ESCALATED`` row.
            ``None`` skips the emit (tests/no-DB environments).
        engine: engine_name for log + payload (e.g. ``"reversion"``).
        ticker: symbol for the structured-log breadcrumb + payload.
        cancel_in_flight: optional zero-arg async callable that
            cancels any orders the engine has already submitted FOR
            THIS TRADE before the execution_risk exception fired.
            Almost never set today (execution_risk runs upstream of
            submit), kept as a hook so a future engine that fires
            tier-1 before tier-2 risk-check can cancel tier-1
            cleanly. Cancel-hook errors are logged + swallowed so the
            escalation path itself can't raise.
        telemetry: extra fields in the ``EXECUTION_RISK_ESCALATED``
            payload (e.g. assessment shape, sizing inputs).

    Returns the plug's decision on success, or ``None`` when:
        * the plug returned ``None`` itself (i.e. trade gated out),
        * the plug raised — in which case the trade is SKIPPED via the
          self-heal path: ``EXECUTION_RISK_ESCALATED`` is emitted,
          ``cancel_in_flight`` is called best-effort, and ``None`` is
          returned so the scheduler advances to the next candidate.

    The exception is NEVER re-raised — the spec says "skip the trade
    (not the whole cycle)" and the helper's job is exactly to honor
    that boundary.
    """
    try:
        result = decide()
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[misc]
        return result  # type: ignore[return-value]
    except Exception as exc:  # noqa: BLE001 — per-trade skip boundary
        # Best-effort cancel of anything in-flight for this trade.
        # An exception here is logged and swallowed — the escalation
        # is the primary signal; the cancel is a safety net.
        if cancel_in_flight is not None:
            try:
                await cancel_in_flight()
            except Exception as cancel_exc:  # noqa: BLE001 — self-heal never raises
                logger.warning(
                    "tpcore.order_management.execution_risk_skip.cancel_failed",
                    engine=engine,
                    ticker=ticker,
                    error=f"{type(cancel_exc).__name__}: {cancel_exc}",
                )
        payload = {
            "schema": 1,
            "engine": engine,
            "ticker": ticker,
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:240],
            **(telemetry or {}),
        }
        await _emit_application_log(
            pool,
            engine=engine,
            event_type=EXECUTION_RISK_ESCALATED_EVENT,
            severity="ERROR",
            message=(
                f"{engine} execution_risk raised for {ticker} — trade "
                f"skipped this cycle ({type(exc).__name__})"
            ),
            payload=payload,
        )
        logger.error(
            "tpcore.order_management.execution_risk_skip.escalated",
            engine=engine,
            ticker=ticker,
            error=type(exc).__name__,
            message=str(exc)[:240],
        )
        return None


__all__ = [
    "EXECUTION_RISK_ESCALATED_EVENT",
    "execute_with_risk_skip",
]
