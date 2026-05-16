"""Adapter for the FINRA Query API — consolidated short interest.

Follows the adapter template: ``@with_retry`` for HTTP, ``structlog``,
env-var config fail-fast, ``httpx.HTTPError`` → ``DataProviderOutage``.

Auth: OAuth2 client-credentials. POST the token endpoint with HTTP
Basic (client_id:secret) → bearer; then POST the dataset endpoint with
``Authorization: Bearer``. Verified 2026-05-16:
``consolidatedShortInterest`` returns rows with ``symbolCode``,
``settlementDate``, ``currentShortPositionQuantity``,
``daysToCoverQuantity``. FINRA does NOT provide float, so
short-interest-% is derived downstream from fundamentals.
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

DEFAULT_TIMEOUT_S = 60.0
_PROVIDER_NAME = "finra"
FINRA_CLIENT_ID_ENV = "FINRA_API_CLIENT_ID"
FINRA_SECRET_ENV = "FINRA_API_SECRET_KEY"
_TOKEN_URL = (
    "https://ews.fip.finra.org/fip/rest/ews/oauth2/access_token"
    "?grant_type=client_credentials"
)
_DATA_URL = (
    "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
)


class ShortInterestRecord(BaseModel):
    """One (ticker, settlement_date) FINRA short-interest observation.

    Raw FINRA fields only — ``short_interest_pct`` is NOT here because
    FINRA gives no float; it is derived in the handler from
    fundamentals shares_outstanding.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    settlement_date: date
    short_position_qty: int
    days_to_cover: Decimal | None


class FinraAdapter:
    """FINRA Query API adapter (consolidated short interest)."""

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._cid = client_id or os.getenv(FINRA_CLIENT_ID_ENV)
        self._secret = client_secret or os.getenv(FINRA_SECRET_ENV)
        if not self._cid or not self._secret:
            raise DataProviderOutage(
                f"{_PROVIDER_NAME} adapter requires {FINRA_CLIENT_ID_ENV} "
                f"+ {FINRA_SECRET_ENV} env vars"
            )
        self._client = client
        self._timeout = timeout
        self._owned_client = client is None
        self._token: str | None = None

    async def __aenter__(self) -> FinraAdapter:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._owned_client = True
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Public API ─────────────────────────────────────────────────────
    async def get_short_interest(
        self, since: date | None = None
    ) -> list[ShortInterestRecord]:
        """Fetch consolidated short interest (optionally settlementDate
        ≥ ``since``). Raises ``DataProviderOutage`` on permanent failure
        or malformed payload."""
        await self._ensure_token()
        body: dict[str, Any] = {}
        if since is not None:
            body = {
                "compareFilters": [{
                    "compareType": "GTE",
                    "fieldName": "settlementDate",
                    "fieldValue": since.isoformat(),
                }]
            }
        try:
            raw = await self._fetch_data(body)
        except DataProviderOutage:
            raise
        except httpx.HTTPError as exc:
            raise DataProviderOutage(
                f"{_PROVIDER_NAME} get_short_interest unreachable: {exc}"
            ) from exc

        out: list[ShortInterestRecord] = []
        try:
            for r in raw:
                sym = r.get("symbolCode")
                sd = r.get("settlementDate")
                if not sym or not sd:
                    continue
                dtc = r.get("daysToCoverQuantity")
                out.append(ShortInterestRecord(
                    ticker=str(sym).upper(),
                    settlement_date=datetime.fromisoformat(str(sd)[:10]).date(),
                    short_position_qty=int(
                        float(r.get("currentShortPositionQuantity") or 0)
                    ),
                    days_to_cover=(Decimal(str(dtc)) if dtc not in (None, "")
                                   else None),
                ))
        except (KeyError, ValueError, TypeError) as exc:
            raise DataProviderOutage(
                f"{_PROVIDER_NAME} malformed short-interest payload: {exc}"
            ) from exc
        logger.info("finra.fetch_done", records=len(out))
        return out

    # ── Internal ───────────────────────────────────────────────────────
    @with_retry(max_attempts=3, backoff_base_sec=2.0, backoff_cap_sec=30.0)
    async def _ensure_token(self) -> None:
        if self._token is not None:
            return
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._owned_client = True
        resp = await self._client.post(_TOKEN_URL, auth=(self._cid, self._secret))
        if resp.status_code == 200:
            self._token = resp.json().get("access_token")
            if not self._token:
                raise DataProviderOutage("finra token endpoint: no access_token")
            return
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            raise httpx.HTTPStatusError(
                f"finra token → {resp.status_code}",
                request=resp.request, response=resp,
            )
        raise DataProviderOutage(
            f"finra token returned {resp.status_code}: {resp.text[:200]}"
        )

    @with_retry(max_attempts=3, backoff_base_sec=2.0, backoff_cap_sec=30.0)
    async def _fetch_data(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._owned_client = True
        resp = await self._client.post(
            _DATA_URL,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=body,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, list) else data.get("data", [])
        if resp.status_code in (401, 403):
            # token may have expired mid-run — clear so a retry re-auths
            self._token = None
        if resp.status_code == 429 or 500 <= resp.status_code < 600 or \
                resp.status_code in (401, 403):
            raise httpx.HTTPStatusError(
                f"finra data → {resp.status_code}",
                request=resp.request, response=resp,
            )
        raise DataProviderOutage(
            f"finra data returned {resp.status_code}: {resp.text[:200]}"
        )


__all__ = [
    "FINRA_CLIENT_ID_ENV",
    "FINRA_SECRET_ENV",
    "FinraAdapter",
    "ShortInterestRecord",
]
