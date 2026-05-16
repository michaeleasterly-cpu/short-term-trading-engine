"""Adapter for the greeks.pro options-analytics provider (free tier).

Follows ``tpcore/templates/adapter_template.py`` exactly: ``@with_retry``
for HTTP, ``structlog`` only, env-var config with fail-fast, raw
``httpx.HTTPError`` mapped to ``DataProviderOutage`` at the boundary.

Rate limits (free tier, verified 2026-05-16): 10 req/min, 600 req/day,
1 tracked symbol. 429 responses carry ``Retry-After`` which
``@with_retry`` honors. Free tier ONLY exposes ``/api/analytics/maxpain``
— ``/flow`` / ``/greeks`` / ``/gex`` are Trader+ and 403 on free (a
verified fact, not an assumption — so this adapter does not implement
them rather than ship code that always 403s).

Live response shape (verified):
``{"symbol","spotPrice","timestamp"(unix s),
   "results":[{"expiration"(unix s),"dte","maxPainStrike",
   "totalPainAtMax","spotDistance","spotDistancePct"}]}``
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, ConfigDict

from tpcore.outage import DataProviderOutage, with_retry

logger = structlog.get_logger(__name__)

DEFAULT_TIMEOUT_S = 30.0

_PROVIDER_NAME = "greeks_pro"
GREEKS_MAXPAIN_ENV = "GREEKS_API_KEY"
_BASE_URL_ENV = "GREEKS_BASE_URL"
_DEFAULT_BASE_URL = "https://greeks.pro"
_MAXPAIN_PATH = "/api/analytics/maxpain"


class MaxPainResult(BaseModel):
    """One expiration's max-pain figures (canonical shape)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expiration_date: datetime
    dte: int
    max_pain_strike: Decimal
    total_pain_at_max: Decimal
    spot_distance: Decimal
    spot_distance_pct: Decimal


class MaxPainSnapshot(BaseModel):
    """A full max-pain snapshot for one symbol at one observation time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    spot_price: Decimal
    observed_at: datetime
    results: list[MaxPainResult]


def _unix_to_utc(ts: Any) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=UTC)


class GreeksProAdapter:
    """Adapter for greeks.pro (free-tier max-pain).

    Args:
        api_key: greeks.pro API key. Defaults to ``GREEKS_API_KEY``.
            Raises ``DataProviderOutage`` if unset — fail-fast.
        base_url: defaults to ``GREEKS_BASE_URL`` env or
            ``https://greeks.pro``.
        client: optional pre-built ``httpx.AsyncClient`` for tests.
        timeout: per-request timeout seconds (default 30).
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_key = api_key or os.getenv(GREEKS_MAXPAIN_ENV)
        if not self._api_key:
            raise DataProviderOutage(
                f"{_PROVIDER_NAME} adapter requires {GREEKS_MAXPAIN_ENV} env var"
            )
        self._base_url = base_url or os.getenv(_BASE_URL_ENV, _DEFAULT_BASE_URL)
        self._client = client
        self._timeout = timeout
        self._owned_client = client is None

    async def __aenter__(self) -> GreeksProAdapter:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout,
            )
            self._owned_client = True
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Public API ─────────────────────────────────────────────────────
    async def get_max_pain(self, symbol: str) -> MaxPainSnapshot:
        """Fetch the current max-pain snapshot for ``symbol``.

        Returns the canonical :class:`MaxPainSnapshot`. Raises
        ``DataProviderOutage`` on permanent failure (incl. 403 = the
        endpoint requires a paid tier; 401 = bad key).
        """
        try:
            raw = await self._fetch_raw(_MAXPAIN_PATH, {"symbol": symbol})
        except DataProviderOutage:
            raise
        except httpx.HTTPError as exc:
            raise DataProviderOutage(
                f"{_PROVIDER_NAME} get_max_pain({symbol}) unreachable: {exc}"
            ) from exc

        try:
            results = [
                MaxPainResult(
                    expiration_date=_unix_to_utc(r["expiration"]),
                    dte=int(r["dte"]),
                    max_pain_strike=Decimal(str(r["maxPainStrike"])),
                    total_pain_at_max=Decimal(str(r["totalPainAtMax"])),
                    spot_distance=Decimal(str(r["spotDistance"])),
                    spot_distance_pct=Decimal(str(r["spotDistancePct"])),
                )
                for r in raw.get("results", [])
            ]
            return MaxPainSnapshot(
                symbol=str(raw["symbol"]),
                spot_price=Decimal(str(raw["spotPrice"])),
                observed_at=_unix_to_utc(raw["timestamp"]),
                results=results,
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise DataProviderOutage(
                f"{_PROVIDER_NAME} get_max_pain({symbol}) malformed payload: {exc}"
            ) from exc

    # ── Internal: HTTP layer ───────────────────────────────────────────
    @with_retry(max_attempts=3, backoff_base_sec=2.0, backoff_cap_sec=30.0)
    async def _fetch_raw(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """One authenticated HTTP GET with retry baked in.

        ``@with_retry`` handles 429 (Retry-After), 5xx, network/timeout.
        4xx-not-429 (401 bad key, 403 paid-tier) is permanent → mapped
        to ``DataProviderOutage`` immediately, no retry.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout,
            )
            self._owned_client = True
        resp = await self._client.get(
            path, params=params, headers={"X-API-Key": self._api_key},
        )
        if resp.status_code == 200:
            logger.debug(
                "greeks_pro.fetch_ok", path=path, bytes=len(resp.content),
            )
            return resp.json()
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


__all__ = [
    "GREEKS_MAXPAIN_ENV",
    "GreeksProAdapter",
    "MaxPainResult",
    "MaxPainSnapshot",
]
