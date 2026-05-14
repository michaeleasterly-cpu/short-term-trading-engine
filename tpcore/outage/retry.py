"""Universal retry decorator for external API calls.

Every adapter and ingestion handler in the platform makes external API
calls (Alpaca, FMP, etc.) that can fail in ways the operator shouldn't
have to babysit:

* HTTP 429 (rate limit) — retry after the server-specified delay.
* HTTP 5xx (server error) — retry with exponential backoff.
* httpx.NetworkError / TimeoutException — transient connectivity.
* Other configurable exception classes per call site.

Before this module, each handler implemented its own ad-hoc
``await asyncio.sleep(1.0)`` loop or — worse — had no retry at all
(``handle_corporate_actions`` was the load-bearing example, observed
failing on Alpaca 429 in production on 2026-05-12).

This decorator is the single source of truth for retry behavior so a
new adapter starts compliant by importing one line.

### Usage

::

    from tpcore.outage.retry import with_retry

    @with_retry(max_attempts=3, backoff_base_sec=2.0)
    async def fetch_something(client, symbol):
        resp = await client.get(f"/data/{symbol}")
        resp.raise_for_status()
        return resp.json()

### Behavior

* Catches ``retry_on`` exceptions (defaults: ``httpx.HTTPStatusError``
  for 429/5xx, ``httpx.NetworkError``, ``httpx.TimeoutException``).
* For 4xx other than 429, raises immediately (no point retrying a 400).
* On HTTP 429/503 with a ``Retry-After`` header, sleeps for the
  server-specified duration (capped at ``backoff_cap_sec``) instead of
  the exponential schedule.
* Exponential backoff: ``backoff_base_sec * 2**(attempt-1)``, capped
  at ``backoff_cap_sec``. Defaults: 2s, 4s, 8s, 16s, capped at 30s.
* Logs at WARNING on each retry with attempt number + delay.
* Logs at ERROR on final failure; re-raises original exception.
"""

from __future__ import annotations

import asyncio
import functools
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx
import structlog

logger = structlog.get_logger(__name__)


# Default exception set worth retrying. Keep narrow — retrying a
# ValueError or KeyError will just mask a real bug.
_DEFAULT_RETRY_ON: tuple[type[BaseException], ...] = (
    httpx.HTTPStatusError,
    httpx.NetworkError,
    httpx.TimeoutException,
)


_F = TypeVar("_F", bound=Callable[..., Awaitable[Any]])


def _is_retryable_status(exc: BaseException) -> bool:
    """Return True if ``exc`` is an HTTP 429 or 5xx (worth retrying).

    HTTPStatusError for 4xx-not-429 is a permanent failure (auth, bad
    request, not found) — retrying just wastes time. Network/timeout
    errors are always retryable.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or 500 <= code < 600
    return True  # non-HTTP errors in retry_on are by definition transient


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Parse the ``Retry-After`` header if present (HTTP 429/503)."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    header = exc.response.headers.get("Retry-After")
    if not header:
        return None
    try:
        return float(header)
    except ValueError:
        # Date-format Retry-After is rare in practice and worth
        # falling back to exponential rather than parsing RFC 7231.
        return None


def with_retry(
    max_attempts: int = 3,
    backoff_base_sec: float = 2.0,
    backoff_cap_sec: float = 30.0,
    retry_on: tuple[type[BaseException], ...] = _DEFAULT_RETRY_ON,
    jitter: bool = True,
) -> Callable[[_F], _F]:
    """Wrap an async function with retry + exponential backoff + Retry-After.

    Args:
        max_attempts: total attempts including the first try. ``3`` means
            "first call + 2 retries" — the most common shape.
        backoff_base_sec: starting delay. Doubled each attempt.
        backoff_cap_sec: ceiling for both exponential and Retry-After
            sleeps. Server-specified longer delays are capped here so a
            misbehaving API can't stall the whole pipeline.
        retry_on: exception classes that trigger a retry. 4xx-not-429
            HTTPStatusErrors bypass retry even if caught (see
            ``_is_retryable_status``).
        jitter: add ±25% jitter to exponential delays so concurrent
            callers don't synchronize their retries (thundering herd).
    """

    def decorator(fn: _F) -> _F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except retry_on as exc:
                    last_exc = exc
                    if not _is_retryable_status(exc):
                        # 4xx-not-429: don't retry.
                        logger.warning(
                            "tpcore.retry.permanent_failure",
                            func=fn.__name__,
                            attempt=attempt,
                            error=type(exc).__name__,
                            status=getattr(getattr(exc, "response", None), "status_code", None),
                        )
                        raise
                    if attempt == max_attempts:
                        logger.error(
                            "tpcore.retry.exhausted",
                            func=fn.__name__,
                            attempts=attempt,
                            error=type(exc).__name__,
                            message=str(exc)[:200],
                        )
                        raise

                    # Pick the sleep duration — Retry-After wins if set.
                    retry_after = _retry_after_seconds(exc)
                    if retry_after is not None:
                        delay = min(retry_after, backoff_cap_sec)
                        source = "retry_after"
                    else:
                        delay = min(
                            backoff_base_sec * (2 ** (attempt - 1)),
                            backoff_cap_sec,
                        )
                        if jitter:
                            delay *= 0.75 + 0.5 * random.random()  # noqa: S311 — non-crypto jitter
                        source = "exponential"
                    logger.warning(
                        "tpcore.retry.attempt",
                        func=fn.__name__,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        delay_sec=round(delay, 2),
                        source=source,
                        error=type(exc).__name__,
                        message=str(exc)[:160],
                    )
                    await asyncio.sleep(delay)
            # Unreachable — the loop either returns, raises permanent,
            # or raises after exhausting attempts. Defensive only.
            assert last_exc is not None
            raise last_exc

        return wrapper  # type: ignore[return-value]

    return decorator


__all__ = ["with_retry"]
