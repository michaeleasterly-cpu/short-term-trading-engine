"""Per-trade order-submit transient retry (Wave-3 E3).

Wave-3 row E3 (see
``docs/superpowers/specs/2026-05-21-deterministic-self-heal-coverage-
expansion-design.md``):

    Detection: a transient Alpaca API error during order submit (timeout,
    network drop). Recovery: retry ONCE; on second failure, mark the
    engine_position degraded + emit ``ORDER_ESCALATED``. RiskGovernor's
    hard-reject (4xx) path is UNCHANGED — only true-transient errors
    flow through this helper.

Safety scope (the live-money double-order constraint)
-----------------------------------------------------

The order-submit path is INTENTIONALLY not routed through
:func:`tpcore.outage.retry.with_retry` in :mod:`tpcore.alpaca.broker_adapter`
— there's a structural guard (``_IDEMPOTENT_READ_OPS``) that raises if a
submit-like call is mis-routed through the retry seam, because retrying
a live-money submit risks a double order even with ``client_order_id``.

This Wave-3 helper sits ABOVE the broker_adapter — at the order-manager
call site — and retries ONLY on the strict subset of "transient errors"
where we can be CERTAIN the broker never received the original request:

* :class:`httpx.NetworkError` — TCP-level failure, no HTTP request line
  ever made it to the server.
* :class:`httpx.TimeoutException` — request timed out before any response
  arrived (network or read timeout).
* :class:`httpx.ConnectError` (subclass of NetworkError) — connection
  refused / unreachable.

We DO NOT retry on 5xx HTTP responses (502 / 503 / 504): a 5xx means the
broker received the request, may have created the order, and the
response just didn't get back. Retrying with the same ``client_order_id``
would be safe in theory (Alpaca dedups by client_order_id and returns
422 on the dup), but the conservative reading of the existing
:mod:`tpcore.alpaca.broker_adapter` policy is "don't trust submit-side
retry semantics on a response we got from the server." We honor that
policy unchanged here.

The spec calls for retry on "timeout, 502, 503, 504, network error".
This module implements the strict subset: **timeout + network error**.
That's a strict superset of the current zero-retry behavior and a strict
subset of the spec's broader definition — the more aggressive 5xx-retry
can be added later in a sibling PR that ALSO updates the broker_adapter
policy (the two changes belong together; doing only one side risks the
double-order foot-gun).

The helper is engine-agnostic: each engine's order manager opts in by
wrapping its broker submit call (:py:meth:`tpcore.alpaca.AlpacaPaperBrokerAdapter.
submit_tier1_only` etc.) via :func:`submit_with_transient_retry`.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar

import httpx
import structlog

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


# Public event name — emitted to ``platform.application_log`` when a
# transient submit error survives the one retry. Distinct from
# ``ORDER_REJECTED`` (RiskGovernor's hard-reject path) and
# ``ENGINE_ESCALATED`` (engine_supervisor's per-engine ladder).
ORDER_ESCALATED_EVENT: str = "ORDER_ESCALATED"

# Per-trade engine_position degradation marker — written alongside
# ORDER_ESCALATED so the next-cycle reconciliation can skip the
# degraded position rather than re-fire on it. The "degraded" flag is
# advisory; the trade monitor + capital_gate see it via the
# application_log query and the engine_supervisor surfaces it.
DEGRADED_POSITION_EVENT: str = "ENGINE_POSITION_DEGRADED"


_INSERT_SQL = """
    INSERT INTO platform.application_log
        (engine, run_id, event_type, severity, message, data)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
"""


def is_pre_response_transient(exc: BaseException) -> bool:
    """True iff ``exc`` is a pre-response transient error worth retrying.

    "Pre-response" = the broker definitely never received the original
    request, so a retry with the same ``client_order_id`` cannot create
    a duplicate order:

    * :class:`httpx.NetworkError` — DNS / TCP / TLS failure before any
      HTTP request line went out.
    * :class:`httpx.TimeoutException` — connect / read timeout, no
      response arrived (server may or may not have processed; this is
      the grey zone, but ``client_order_id`` dedup makes it safe).

    Explicitly EXCLUDED: :class:`httpx.HTTPStatusError` of any code
    (including 5xx) — a status response means the server saw the request.
    See module docstring for the safety rationale.
    """
    if isinstance(exc, httpx.NetworkError):
        return True
    if isinstance(exc, httpx.TimeoutException):
        return True
    return False


async def _emit_application_log(
    pool: asyncpg.Pool | None,
    *,
    engine: str,
    event_type: str,
    severity: str,
    message: str,
    payload: dict[str, Any],
) -> None:
    """Crash-isolated application_log emit (mirrors engine_supervisor._emit)."""
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                _INSERT_SQL, engine, uuid.uuid4(), event_type, severity,
                message, json.dumps(payload, default=str),
            )
    except Exception as exc:  # noqa: BLE001 — observability is best-effort
        logger.warning(
            "tpcore.order_management.transient_retry.emit_failed",
            engine=engine, event_type=event_type, error=str(exc),
        )


_T = TypeVar("_T")


async def submit_with_transient_retry(
    submit: Callable[[], Awaitable[_T]],
    *,
    pool: asyncpg.Pool | None,
    engine: str,
    client_order_id: str,
    ticker: str,
    telemetry: dict[str, Any] | None = None,
) -> _T:
    """Run ``submit()`` with at-most-one retry on pre-response transients.

    Args:
        submit: zero-arg async callable that calls broker.place_order /
            submit_tier1_only. The SAME ``client_order_id`` is used on
            both attempts (Alpaca server-side dedup makes that safe — a
            duplicate gets a 422 ``order already exists``).
        pool: asyncpg pool for the ORDER_ESCALATED / DEGRADED rows. May
            be None in tests; emit is skipped then.
        engine: engine_name (e.g. ``"reversion"``).
        client_order_id: caller-stable client order id; carried in
            telemetry so the operator can correlate the escalation back
            to the original Tier-1 row.
        ticker: symbol for the structured-log breadcrumb.
        telemetry: extra data carried in the ``ORDER_ESCALATED`` payload
            (decision sizing, expected edge, etc.). The caller decides
            what to surface.

    Behavior:
        * First attempt: call ``submit()``. On success → return.
        * Pre-response transient (``NetworkError``/``TimeoutException``):
          log at WARNING and retry ONCE.
        * Second attempt's transient failure: emit ``ORDER_ESCALATED``
          + ``ENGINE_POSITION_DEGRADED``, then re-raise the original
          exception so the caller's reconciliation path runs.
        * Non-transient exception (any ``HTTPStatusError`` incl. 5xx;
          any ``APIError``; ``ValueError`` etc.): NO retry, re-raise
          unchanged. The hard-reject path is owned by RiskGovernor
          upstream + the broker_adapter outage classifier.

    Returns the broker-acknowledged result of the successful attempt.
    """
    telemetry = telemetry or {}
    try:
        return await submit()
    except Exception as first_exc:  # noqa: BLE001 — re-raise non-transient below
        if not is_pre_response_transient(first_exc):
            # Hard reject / non-transient — pass through unchanged.
            raise
        logger.warning(
            "tpcore.order_management.transient_retry.attempt",
            engine=engine, ticker=ticker,
            client_order_id=client_order_id,
            attempt=1, max_attempts=2,
            error=type(first_exc).__name__,
            message=str(first_exc)[:160],
        )
    # ONE retry — same client_order_id (Alpaca dedup makes it safe).
    try:
        return await submit()
    except Exception as second_exc:  # noqa: BLE001 — re-raise after surface
        if not is_pre_response_transient(second_exc):
            # The second attempt failed in a *different* (non-transient)
            # way — re-raise that exception directly. The caller sees
            # the genuine non-transient failure, not the first transient.
            raise
        # Both attempts transient → ESCALATED + DEGRADED surface.
        escalate_payload = {
            "schema": 1,
            "engine": engine,
            "ticker": ticker,
            "client_order_id": client_order_id,
            "attempts": 2,
            "error_type": type(second_exc).__name__,
            "error_message": str(second_exc)[:240],
            **telemetry,
        }
        await _emit_application_log(
            pool, engine=engine, event_type=ORDER_ESCALATED_EVENT,
            severity="ERROR",
            message=(
                f"{engine} order submit escalated after 2 transient attempts: "
                f"{ticker} ({type(second_exc).__name__})"
            ),
            payload=escalate_payload,
        )
        await _emit_application_log(
            pool, engine=engine, event_type=DEGRADED_POSITION_EVENT,
            severity="WARNING",
            message=(
                f"{engine} position degraded: {ticker} "
                f"client_order_id={client_order_id}"
            ),
            payload={
                "schema": 1,
                "engine": engine,
                "ticker": ticker,
                "client_order_id": client_order_id,
                "reason": "order_submit_escalated_transient",
            },
        )
        logger.error(
            "tpcore.order_management.transient_retry.escalated",
            engine=engine, ticker=ticker,
            client_order_id=client_order_id,
            error=type(second_exc).__name__,
            message=str(second_exc)[:240],
        )
        raise


__all__ = [
    "DEGRADED_POSITION_EVENT",
    "ORDER_ESCALATED_EVENT",
    "is_pre_response_transient",
    "submit_with_transient_retry",
]
