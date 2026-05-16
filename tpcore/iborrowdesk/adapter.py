"""Adapter for IBorrowDesk borrow rates (no auth, scrape-fragile).

Follows the adapter template: ``@with_retry`` for HTTP, ``structlog``,
``httpx.HTTPError`` → ``DataProviderOutage``. No API key.

``GET https://www.iborrowdesk.com/api/ticker/<SYM>`` →
``{"daily":[{"date":"YYYY-MM-DD","fee":<pct>,"available":<int>,...}],
   ...}``. ``fee`` is the borrow rate %. Per-ticker; the handler loops
the T1/T2 universe. 429/5xx/network retry via the canonical
``with_retry`` decorator; a 403 scrape-block is permanent per
``_is_retryable_status`` and surfaces as ``DataProviderOutage`` —
the handler counts it (3 consecutive → CRITICAL skip, never crash).
"""
from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, ConfigDict

from tpcore.outage import DataProviderOutage, with_retry

logger = structlog.get_logger(__name__)

DEFAULT_TIMEOUT_S = 30.0
_PROVIDER_NAME = "iborrowdesk"
_BASE_URL_ENV = "IBORROWDESK_BASE_URL"
_DEFAULT_BASE_URL = "https://www.iborrowdesk.com"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class BorrowRateRecord(BaseModel):
    """One (ticker, date) borrow-rate point (canonical shape)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    date: date
    borrow_rate_pct: Decimal

    @classmethod
    def from_raw(cls, ticker: str, d: Any, fee: Any) -> BorrowRateRecord:
        return cls(
            ticker=ticker.upper(),
            date=datetime.fromisoformat(str(d)[:10]).date(),
            borrow_rate_pct=Decimal(str(fee)),
        )


class IBorrowDeskAdapter:
    """IBorrowDesk borrow-rate adapter (no-auth, per-ticker)."""

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

    async def __aenter__(self) -> IBorrowDeskAdapter:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout,
                headers={"User-Agent": _UA, "Accept": "application/json"},
                follow_redirects=True,
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
    async def get_latest_borrow_rate(self, ticker: str) -> BorrowRateRecord | None:
        """Most-recent borrow-rate point for ``ticker``, or ``None`` if
        the symbol has no data. Raises ``DataProviderOutage`` on a
        permanent/structural failure (the handler catches & counts so a
        scrape block never crashes the pipeline)."""
        try:
            raw = await self._fetch_raw(f"/api/ticker/{ticker.upper()}")
        except DataProviderOutage:
            raise
        except httpx.HTTPError as exc:
            raise DataProviderOutage(
                f"{_PROVIDER_NAME} get_latest_borrow_rate({ticker}) "
                f"unreachable: {exc}"
            ) from exc
        try:
            daily = raw.get("daily") or []
            if not daily:
                return None  # legit "no data" (unknown/untracked symbol)
            latest = max(daily, key=lambda d: str(d.get("date", "")))
            if latest.get("date") is None or latest.get("fee") is None:
                # Non-empty feed whose newest row is structurally broken
                # is a malformed payload, NOT "no data" — fail loudly so
                # the handler counts it (3 consecutive → skip, never
                # silently persist a corrupt rate).
                raise DataProviderOutage(
                    f"{_PROVIDER_NAME} malformed payload for {ticker}: "
                    f"newest daily row missing date/fee: {latest!r}"
                )
            return BorrowRateRecord.from_raw(ticker, latest["date"], latest["fee"])
        except DataProviderOutage:
            raise
        except (KeyError, ValueError, TypeError) as exc:
            raise DataProviderOutage(
                f"{_PROVIDER_NAME} malformed payload for {ticker}: {exc}"
            ) from exc

    # ── Internal ───────────────────────────────────────────────────────
    @with_retry(max_attempts=3, backoff_base_sec=2.0, backoff_cap_sec=30.0)
    async def _fetch_raw(self, path: str) -> dict[str, Any]:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout,
                headers={"User-Agent": _UA, "Accept": "application/json"},
                follow_redirects=True,
            )
            self._owned_client = True
        resp = await self._client.get(path)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404:
            return {"daily": []}  # unknown ticker — not an error
        # 403/429/5xx → retry (scrape blocks are transient-ish)
        if resp.status_code in (403, 429) or 500 <= resp.status_code < 600:
            raise httpx.HTTPStatusError(
                f"{_PROVIDER_NAME} {path} → {resp.status_code}",
                request=resp.request, response=resp,
            )
        raise DataProviderOutage(
            f"{_PROVIDER_NAME} {path} returned {resp.status_code}: "
            f"{resp.text[:160]}"
        )


__all__ = ["BorrowRateRecord", "IBorrowDeskAdapter"]
