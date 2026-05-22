"""Shared transient-DB retry helper for engine setup_detection plugs (E2).

Wave-3 row E2 (see
``docs/superpowers/specs/2026-05-21-deterministic-self-heal-coverage-
expansion-design.md``):

    Each engine's ``setup_detection`` plug fetches a panel of bars / market
    context off ``platform.prices_daily`` via the
    :class:`tpcore.interfaces.data.DataProviderInterface`. A transient
    pool blip (Supabase Supavisor pooler hiccup, momentary network drop,
    asyncpg ``ConnectionDoesNotExistError``) used to escalate straight
    out of the plug — the engine cycle was skipped even though the
    panel-load would have succeeded on the very next attempt.

This helper wraps a single panel-load callable with the same retry shape
PR #163 used at the chunk-fetch level, but lifted to the *plug* call
site so engines can opt in by wrapping ``self._data.get_daily_bars`` (or
any sibling fetch) without each duplicating retry semantics.

Design notes
------------

* **Three attempts** with exponential backoff (1s → 2s, capped 10s) +
  ±25% jitter — mirrors :func:`tpcore.outage.retry.with_retry` so the
  engine-lane behavior matches the data-lane.
* **Narrow transient class**: only :class:`asyncpg.exceptions.
  PostgresConnectionError` subclasses + the ``InterfaceError`` /
  ``PoolTimeout`` family. A planner-canceled query
  (:class:`asyncpg.exceptions.QueryCanceledError`) is also transient
  (statement_timeout window flapping). A genuine programming error
  (:class:`asyncpg.exceptions.PostgresSyntaxError`,
  :class:`asyncpg.exceptions.UndefinedTableError`) is **not** retried —
  retrying a syntax error just delays the loud crash.
* **Lazy asyncpg import**: this module is imported by engine plug
  modules before the asyncpg pool is wired, and the test surface
  monkeypatches the retry behavior, so the actual ``asyncpg.exceptions``
  reference must be resolved at call time. Mirrors the
  :mod:`tpcore.data.batched_fetchers` lazy-import precedent.

Pilot wiring (this PR): :class:`reversion.plugs.setup_detection.
ReversionSetupDetection`. The remaining 5 PAPER engines (vector,
momentum, sentinel, canary, catalyst) wire on the same call-site shape
in a Wave-3b PR — the helper is engine-agnostic by construction.
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

import structlog

logger = structlog.get_logger(__name__)


# Defaults match the spec ("3 attempts with exponential backoff") and
# mirror tpcore.outage.retry. Exposed as module constants so call sites
# can reference the canonical numbers and tests can monkeypatch
# behavior-preserving.
DEFAULT_MAX_ATTEMPTS: int = 3
DEFAULT_BACKOFF_BASE_SEC: float = 1.0
DEFAULT_BACKOFF_CAP_SEC: float = 10.0


# Asyncpg exception classes that count as transient. Imported lazily so
# this module has no hard dep at load time (mirrors batched_fetchers).
# The frozenset is rebuilt per-call via ``_transient_class_names`` so a
# test monkeypatch on ``asyncpg.exceptions`` is honored without module-
# load-time caching.
_TRANSIENT_NAMES: tuple[str, ...] = (
    # connection-class transient errors
    "ConnectionDoesNotExistError",
    "InterfaceError",
    "InternalClientError",
    # pool-class transient errors
    "TooManyConnectionsError",
    # query-cancel: statement_timeout / pool-side cancel (the
    # planner-canceled flavor — same class batched_fetchers retries)
    "QueryCanceledError",
    # network blip on the libpq side
    "ConnectionFailureError",
    "PostgresConnectionError",
)


def is_transient_db_error(exc: BaseException) -> bool:
    """True iff ``exc`` is a transient asyncpg error worth retrying.

    The check is by class NAME (not isinstance) so a test fake that
    raises an asyncpg-look-alike exception with the matching class name
    is retried like the real thing. The wider isinstance check would
    require importing ``asyncpg.exceptions`` and a stable class identity
    that tests can monkeypatch — by-name matching dodges both concerns
    without changing real-world behavior (the asyncpg classes' names are
    a stable public API).
    """
    name = type(exc).__name__
    if name in _TRANSIENT_NAMES:
        return True
    # asyncio.TimeoutError counts too — pool.acquire() with timeout=10
    # surfaces as TimeoutError when the pool is exhausted; the next try
    # may succeed once a slot frees.
    if isinstance(exc, asyncio.TimeoutError):
        return True
    # Walk the MRO so subclasses of the asyncpg base errors (which all
    # derive from PostgresError) are caught. Avoids needing a hard
    # asyncpg import at module load.
    for cls in type(exc).__mro__:
        if cls.__name__ in _TRANSIENT_NAMES:
            return True
    return False


_T = TypeVar("_T")


async def fetch_with_transient_retry(
    fetch: Callable[[], Awaitable[_T]],
    *,
    engine: str,
    op: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base_sec: float = DEFAULT_BACKOFF_BASE_SEC,
    backoff_cap_sec: float = DEFAULT_BACKOFF_CAP_SEC,
    jitter: bool = True,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    _rand: Callable[[], float] = random.random,
) -> _T:
    """Run ``fetch()`` with transient-DB retry + exponential backoff.

    Args:
        fetch: zero-arg async callable that performs the panel-load.
            Wrap a bound method or a closure to bind real args (the
            helper is one-shot per call so each retry re-runs ``fetch``
            from scratch — a stateful method would be ill-behaved here).
        engine: engine_name (for structured log breadcrumbs only).
        op: human-readable operation label (e.g. ``"get_daily_bars"``).
        max_attempts: total attempts including the first. Default ``3``
            per spec.
        backoff_base_sec, backoff_cap_sec: same shape as
            ``tpcore.outage.retry.with_retry`` (``base * 2**(n-1)`` then
            capped at ``cap``).
        jitter: ±25% multiplicative jitter to avoid thundering herd if
            multiple plugs retry simultaneously.
        sleep, _rand: injection seams for deterministic tests.

    Behavior:
        * On a transient asyncpg error (see :func:`is_transient_db_error`),
          logs at WARNING and retries after the backoff sleep.
        * On a non-transient exception, re-raises immediately — no point
          retrying a syntax error / undefined table.
        * On the final attempt's transient failure, re-raises the original
          exception (the caller's try/except sees the same shape it would
          without this helper) and logs at ERROR.

    NOT a decorator on purpose: engine plugs call this once per panel-load
    site so the wrap point is visible at the call site (matches PR #163's
    per-chunk retry pattern at the call-site level).
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fetch()
        except Exception as exc:  # noqa: BLE001 — re-raise non-transient below
            if not is_transient_db_error(exc):
                # Non-transient — re-raise immediately. NOT a retry case.
                raise
            last_exc = exc
            if attempt == max_attempts:
                logger.error(
                    "tpcore.engine.transient_retry.exhausted",
                    engine=engine, op=op, attempts=attempt,
                    error=type(exc).__name__, message=str(exc)[:200],
                )
                raise
            delay = min(
                backoff_base_sec * (2 ** (attempt - 1)),
                backoff_cap_sec,
            )
            if jitter:
                delay *= 0.75 + 0.5 * _rand()
            logger.warning(
                "tpcore.engine.transient_retry.attempt",
                engine=engine, op=op, attempt=attempt,
                max_attempts=max_attempts,
                delay_sec=round(delay, 3),
                error=type(exc).__name__, message=str(exc)[:160],
            )
            await sleep(delay)
    # Unreachable — the loop either returns, re-raises permanent, or
    # re-raises after exhausting attempts. Defensive only.
    assert last_exc is not None
    raise last_exc


__all__ = [
    "DEFAULT_BACKOFF_BASE_SEC",
    "DEFAULT_BACKOFF_CAP_SEC",
    "DEFAULT_MAX_ATTEMPTS",
    "fetch_with_transient_retry",
    "is_transient_db_error",
]
