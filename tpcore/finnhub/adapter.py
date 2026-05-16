"""Adapter for Finnhub insider-sentiment (free tier).

Follows ``tpcore/templates/adapter_template.py``: ``@with_retry`` for
HTTP, ``structlog`` only, env-var config with fail-fast, raw
``httpx.HTTPError`` mapped to ``DataProviderOutage`` at the boundary.

Free tier exposes ``/stock/insider-sentiment`` only. ``/news-sentiment``
and ``/stock/social-sentiment`` are premium (403 on free, verified
2026-05-16) — deliberately not implemented rather than ship code that
always 403s.

Live response shape (verified):
``{"symbol":"AAPL","data":[{"symbol","year","month","change","mspr"}]}``
where ``mspr`` ∈ [-100, 100] (Monthly Share Purchase Ratio — insider
sentiment) and ``change`` is the net insider share change for the month.
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, ConfigDict

from tpcore.outage import DataProviderOutage, with_retry

logger = structlog.get_logger(__name__)

DEFAULT_TIMEOUT_S = 30.0

_PROVIDER_NAME = "finnhub"
FINNHUB_API_KEY_ENV = "FINNHUB_API_KEY"
_BASE_URL_ENV = "FINNHUB_BASE_URL"
_DEFAULT_BASE_URL = "https://finnhub.io/api/v1"
_INSIDER_SENTIMENT_PATH = "/stock/insider-sentiment"


class InsiderSentimentRecord(BaseModel):
    """One (symbol, year, month) insider-sentiment observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    year: int
    month: int
    mspr: Decimal          # Monthly Share Purchase Ratio, [-100, 100]
    net_change: Decimal    # net insider share change for the month


class InsiderSentimentResult(BaseModel):
    """All monthly insider-sentiment records for one symbol/window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    records: list[InsiderSentimentRecord]


class FinnhubAdapter:
    """Adapter for Finnhub (free-tier insider sentiment).

    Args:
        api_key: Finnhub key. Defaults to ``FINNHUB_API_KEY``. Raises
            ``DataProviderOutage`` if unset — fail-fast.
        base_url: defaults to ``FINNHUB_BASE_URL`` env or
            ``https://finnhub.io/api/v1``.
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
        self._api_key = api_key or os.getenv(FINNHUB_API_KEY_ENV)
        if not self._api_key:
            raise DataProviderOutage(
                f"{_PROVIDER_NAME} adapter requires {FINNHUB_API_KEY_ENV} env var"
            )
        self._base_url = base_url or os.getenv(_BASE_URL_ENV, _DEFAULT_BASE_URL)
        self._client = client
        self._timeout = timeout
        self._owned_client = client is None

    async def __aenter__(self) -> FinnhubAdapter:
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
    async def get_insider_sentiment(
        self, symbol: str, from_date: date, to_date: date
    ) -> InsiderSentimentResult:
        """Fetch monthly insider-sentiment (MSPR) for ``symbol``.

        Returns the canonical :class:`InsiderSentimentResult`. Raises
        ``DataProviderOutage`` on permanent failure (401 bad key, 403
        premium-only endpoint, malformed payload).
        """
        try:
            raw = await self._fetch_raw(
                _INSIDER_SENTIMENT_PATH,
                {
                    "symbol": symbol,
                    "from": from_date.isoformat(),
                    "to": to_date.isoformat(),
                },
            )
        except DataProviderOutage:
            raise
        except httpx.HTTPError as exc:
            raise DataProviderOutage(
                f"{_PROVIDER_NAME} get_insider_sentiment({symbol}) "
                f"unreachable: {exc}"
            ) from exc

        try:
            records = [
                InsiderSentimentRecord(
                    symbol=str(d.get("symbol", symbol)),
                    year=int(d["year"]),
                    month=int(d["month"]),
                    mspr=Decimal(str(d["mspr"])),
                    net_change=Decimal(str(d["change"])),
                )
                for d in raw.get("data", [])
            ]
            return InsiderSentimentResult(symbol=symbol, records=records)
        except (KeyError, ValueError, TypeError) as exc:
            raise DataProviderOutage(
                f"{_PROVIDER_NAME} get_insider_sentiment({symbol}) "
                f"malformed payload: {exc}"
            ) from exc

    # ── Internal: HTTP layer ───────────────────────────────────────────
    @with_retry(max_attempts=3, backoff_base_sec=2.0, backoff_cap_sec=30.0)
    async def _fetch_raw(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """One authenticated HTTP GET with retry baked in.

        ``@with_retry`` handles 429 (Retry-After), 5xx, network/timeout.
        4xx-not-429 (401 bad key, 403 premium) is permanent → mapped to
        ``DataProviderOutage`` immediately, no retry.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout,
            )
            self._owned_client = True
        resp = await self._client.get(
            path, params={**params, "token": self._api_key},
        )
        if resp.status_code == 200:
            logger.debug(
                "finnhub.fetch_ok", path=path, bytes=len(resp.content),
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
    "FINNHUB_API_KEY_ENV",
    "FinnhubAdapter",
    "InsiderSentimentRecord",
    "InsiderSentimentResult",
]
