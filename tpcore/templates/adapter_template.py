"""Template for a new external-API data adapter.

Copy this file to ``tpcore/<provider>/<name>_adapter.py`` and replace
the placeholders. Every adapter on the platform should follow this
exact shape — error handling, logging, config, retry, and outage
classification are uniform so a future operator can read any adapter
and know what to expect.

This template compiles standalone (the imports are all real) but the
fetch methods raise ``NotImplementedError`` until you wire them.

### Conventions

1. **Retry**: every method that makes an HTTP call uses
   ``@with_retry`` from ``tpcore.outage``. Never write a local
   ``await asyncio.sleep(1.0)`` retry loop — the decorator handles
   exponential backoff, Retry-After, and 429/5xx-only retries.

2. **Logging**: ``structlog`` only — never ``print``, never the
   stdlib ``logging`` module. Log at INFO for successful operations
   with structured context (``ticker=``, ``endpoint=``, etc.). The
   ``@with_retry`` decorator handles WARNING/ERROR for retries +
   exhaustion automatically.

3. **Configuration**: read API keys and base URLs from environment
   variables via ``os.getenv``. Never hardcode credentials. Raise
   ``DataProviderOutage`` at construction time if a required env
   var is missing — failing late is worse than failing fast.

4. **Outage mapping**: catch ``httpx.HTTPError`` at the boundary,
   raise ``DataProviderOutage`` with the provider name + endpoint +
   short message. Engines treat this as a hard "no data, no trade"
   signal. Don't bubble raw httpx exceptions into engine code.

5. **Interface compliance**: if the adapter implements an ABC (e.g.,
   ``DataProviderInterface``), every abstract method has a real
   implementation OR raises ``NotImplementedError`` with a docstring
   explaining why and what future work is needed.

6. **Tests**: every adapter has a ``tpcore/tests/test_<name>_adapter.py``
   that exercises:
   * happy path against a mocked ``httpx.MockTransport``;
   * retry-on-429 and no-retry-on-403 (cross-checks the decorator);
   * outage mapping on permanent failure;
   * config error at construction.

See ``docs/superpowers/checklists/adapter_readiness.md`` for the
pre-merge checklist.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import structlog

from tpcore.outage import DataProviderOutage, with_retry

logger = structlog.get_logger(__name__)


# ── Configuration constants ────────────────────────────────────────────
# Base URL and credentials come from env vars. Defaults are safe (raise
# on use, not on import) so the test suite can import this module
# without the env vars being set.

DEFAULT_TIMEOUT_S = 30.0

# Replace these with your provider's actual env-var names + base URL.
_PROVIDER_NAME = "REPLACE_ME"  # e.g. "fmp", "fred", "iborrowdesk"
_BASE_URL_ENV = "REPLACE_ME_BASE_URL"
_API_KEY_ENV = "REPLACE_ME_API_KEY"
_DEFAULT_BASE_URL = "https://api.example.com/v1"


class ExampleAdapter:
    """Adapter for the REPLACE_ME provider.

    Args:
        api_key: provider API key. Defaults to the ``REPLACE_ME_API_KEY``
            environment variable. Raises ``DataProviderOutage`` if neither
            is set — fail-fast at construction.
        base_url: API base URL. Defaults to ``REPLACE_ME_BASE_URL`` env or
            ``_DEFAULT_BASE_URL``.
        client: optional pre-built ``httpx.AsyncClient`` for tests.
        timeout: per-request timeout in seconds. Defaults to 30.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_key = api_key or os.getenv(_API_KEY_ENV)
        if not self._api_key:
            raise DataProviderOutage(
                f"{_PROVIDER_NAME} adapter requires {_API_KEY_ENV} env var"
            )
        self._base_url = base_url or os.getenv(_BASE_URL_ENV, _DEFAULT_BASE_URL)
        self._client = client
        self._timeout = timeout
        self._owned_client = client is None

    async def __aenter__(self) -> ExampleAdapter:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout,
            )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Public API ─────────────────────────────────────────────────────
    async def get_thing(self, identifier: str) -> dict[str, Any]:
        """Fetch a single thing by identifier.

        Returns the provider's JSON payload mapped to the platform's
        canonical shape (drop the provider-specific field names, use
        the platform's vocabulary). Raises ``DataProviderOutage`` on
        permanent failure.
        """
        try:
            return await self._fetch_raw(f"/things/{identifier}")
        except DataProviderOutage:
            raise
        except httpx.HTTPError as exc:
            raise DataProviderOutage(
                f"{_PROVIDER_NAME} get_thing({identifier}) unreachable: {exc}"
            ) from exc

    # ── Internal: HTTP layer ───────────────────────────────────────────
    @with_retry(max_attempts=3, backoff_base_sec=2.0, backoff_cap_sec=30.0)
    async def _fetch_raw(self, path: str) -> dict[str, Any]:
        """One HTTP GET with retry baked in.

        ``@with_retry`` handles 429 (with Retry-After), 5xx, NetworkError,
        and TimeoutException. 4xx-not-429 is permanent and re-raised
        without retry — the outer ``get_thing`` maps it to
        ``DataProviderOutage``.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout,
            )
            self._owned_client = True
        params = {"apikey": self._api_key}
        resp = await self._client.get(path, params=params)
        if resp.status_code == 200:
            logger.debug(
                f"{_PROVIDER_NAME}.fetch_ok",
                path=path,
                bytes=len(resp.content),
            )
            return resp.json()
        # 429/5xx → raise so @with_retry retries.
        # 4xx-not-429 → DataProviderOutage immediately (permanent).
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            raise httpx.HTTPStatusError(
                f"{_PROVIDER_NAME} {path} → {resp.status_code}",
                request=resp.request,
                response=resp,
            )
        raise DataProviderOutage(
            f"{_PROVIDER_NAME} {path} returned {resp.status_code}: "
            f"{resp.text[:200]}"
        )


__all__ = ["ExampleAdapter"]
