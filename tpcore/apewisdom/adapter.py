"""Adapter for ApeWisdom social sentiment (no auth, paginated).

Follows ``tpcore/templates/adapter_template.py``: ``@with_retry`` for
HTTP, ``structlog`` only, ``httpx.HTTPError`` → ``DataProviderOutage``
at the boundary. No API key (public endpoint) so there is no
fail-fast-on-key; base URL is still env-overridable for tests.

``/filter/all-stocks?page=N`` returns
``{"count","pages","current_page","results":[{rank,ticker,name,
mentions,upvotes,rank_24h_ago,mentions_24h_ago}]}``. Paginate to
``pages``; ~1-2 req/s courtesy (rate limits undocumented).
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, ConfigDict

from tpcore.outage import DataProviderOutage, with_retry

logger = structlog.get_logger(__name__)

DEFAULT_TIMEOUT_S = 30.0
_PROVIDER_NAME = "apewisdom"
_BASE_URL_ENV = "APEWISDOM_BASE_URL"
_DEFAULT_BASE_URL = "https://apewisdom.io/api/v1.0"
_FILTER_PATH = "/filter/all-stocks"
_PAGE_COURTESY_S = 0.6  # ~1.6 req/s — polite, well under any sane limit
_MAX_PAGES = 50         # hard safety cap (~5k tickers); API is ~12 pages


class SocialSentimentRecord(BaseModel):
    """One ticker's current social-sentiment snapshot (canonical shape)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    name: str
    rank: int
    mentions: int
    upvotes: int
    rank_24h_ago: int | None
    mentions_24h_ago: int | None


class ApeWisdomAdapter:
    """Adapter for ApeWisdom (no-auth social sentiment).

    Args:
        base_url: defaults to ``APEWISDOM_BASE_URL`` env or
            ``https://apewisdom.io/api/v1.0``.
        client: optional pre-built ``httpx.AsyncClient`` for tests.
        timeout: per-request timeout seconds (default 30).
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._base_url = base_url or os.getenv(_BASE_URL_ENV, _DEFAULT_BASE_URL)
        self._client = client
        self._timeout = timeout
        self._owned_client = client is None

    async def __aenter__(self) -> ApeWisdomAdapter:
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
    async def get_all_sentiment(self) -> list[SocialSentimentRecord]:
        """Fetch every page and return all ticker sentiment records.

        Raises ``DataProviderOutage`` on permanent failure or a
        malformed payload.
        """
        out: list[SocialSentimentRecord] = []
        page = 1
        total_pages = 1
        while page <= total_pages and page <= _MAX_PAGES:
            try:
                # ApeWisdom paginates via a PATH segment, NOT ?page= —
                # the query form is silently ignored (returns page 1
                # every time). Verified 2026-05-16.
                raw = await self._fetch_raw(f"{_FILTER_PATH}/page/{page}", {})
            except DataProviderOutage:
                raise
            except httpx.HTTPError as exc:
                raise DataProviderOutage(
                    f"{_PROVIDER_NAME} get_all_sentiment p{page} unreachable: {exc}"
                ) from exc
            try:
                total_pages = int(raw["pages"])
                for r in raw.get("results", []):
                    out.append(SocialSentimentRecord(
                        ticker=str(r["ticker"]).upper(),
                        name=str(r.get("name", "")),
                        rank=int(r["rank"]),
                        mentions=int(r["mentions"]),
                        upvotes=int(r["upvotes"]),
                        rank_24h_ago=(int(r["rank_24h_ago"])
                                      if r.get("rank_24h_ago") is not None else None),
                        mentions_24h_ago=(int(r["mentions_24h_ago"])
                                          if r.get("mentions_24h_ago") is not None else None),
                    ))
            except (KeyError, ValueError, TypeError) as exc:
                raise DataProviderOutage(
                    f"{_PROVIDER_NAME} malformed payload (page {page}): {exc}"
                ) from exc
            page += 1
            if page <= total_pages:
                await asyncio.sleep(_PAGE_COURTESY_S)
        logger.info(
            "apewisdom.fetch_done", records=len(out), pages=total_pages,
        )
        return out

    # ── Internal: HTTP layer ───────────────────────────────────────────
    @with_retry(max_attempts=3, backoff_base_sec=2.0, backoff_cap_sec=30.0)
    async def _fetch_raw(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """One HTTP GET with retry. 429/5xx → retry; 4xx-not-429 →
        permanent ``DataProviderOutage``."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout,
            )
            self._owned_client = True
        resp = await self._client.get(path, params=params)
        if resp.status_code == 200:
            logger.debug("apewisdom.fetch_ok", path=path,
                         page=params.get("page"), bytes=len(resp.content))
            return resp.json()
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            raise httpx.HTTPStatusError(
                f"{_PROVIDER_NAME} {path} → {resp.status_code}",
                request=resp.request, response=resp,
            )
        raise DataProviderOutage(
            f"{_PROVIDER_NAME} {path} returned {resp.status_code}: "
            f"{resp.text[:200]}"
        )


__all__ = ["ApeWisdomAdapter", "SocialSentimentRecord"]
