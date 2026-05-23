"""OpenFIGI v3 mapping adapter — ticker → US Composite FIGI.

**Provider:** Bloomberg OpenFIGI (open standard, free with API key).
**Docs:** https://www.openfigi.com/api/documentation
**Ontology:** OMG FIGI 1.2 — https://www.omg.org/spec/FIGI/1.2

**Endpoint used:** `POST https://api.openfigi.com/v3/mapping`

**Rate-limit class (with `X-OPENFIGI-APIKEY`):** 25 requests per 6 seconds,
up to 100 jobs per request → ~25,000 mappings/min. Free-tier (no key):
25 req/min × 10 jobs/req. We always require a key; fail-fast at construction
if the env var is missing.

**Auth env var:** `OPEN_FIGI_API_KEY` (underscore between OPEN and FIGI).

**Per v2.2 spec §1.8:** event-driven; called by `parent_resolver` on
`UNKNOWN_TICKER_OBSERVED`. Not a scheduled cron feed.

**Per v2.2 spec §1.9:** the FIGI level we STORE is the `compositeFIGI`
(per-jurisdiction; for ADRs identifies the ADR specifically; stable across
US exchange transfers). The other two levels (`figi` exchange-level and
`shareClassFIGI` global) are returned but not stored.

**Per v2.2 spec §1.10 + sibling memory `sec-primary-insider-fmp-fallback-non-us`:**
this adapter is one source among several in `parent_resolver`'s dispatch chain.
SEC `company_tickers.json` is primary for insider/CIK lookups on US;
FMP `/profile` is primary for country/asset_class/exchange enrichment;
OpenFIGI is the source for FIGI specifically — used by every lane.

**Per v2.2 spec §1.2 (OMG ontology):** every returned FIGI is validated
against the canonical regex before storing — defense against vendor anomaly.
"""
from __future__ import annotations

import os
import re
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, ConfigDict

from tpcore.outage import DataProviderOutage, with_retry

logger = structlog.get_logger(__name__)

OPENFIGI_BASE_URL = "https://api.openfigi.com"
"""Production OpenFIGI API base URL. Override via env `OPENFIGI_BASE_URL` only for tests."""

# Per OMG FIGI 1.2 ontology (`GlobalInstrumentIdentifiers.rdf`): a valid FIGI is
# 12 chars — two consonants (not vowels, not the prohibited country prefixes
# BS/BM/GG/GB/VG/GH/KY which collide with CUSIP/ISIN namespace) + literal 'G' +
# 8 alphanumerics (no vowels) + 1 check digit.
OPENFIGI_FIGI_REGEX = re.compile(
    r"^(?!BS|BM|GG|GB|VG|GH|KY)"
    r"[BCDFGHJKLMNPQRSTVWXZ]{2}"
    r"G"
    r"[BCDFGHJKLMNPQRSTVWXYZ0-9]{8}"
    r"\d$"
)

# OpenFIGI accepts up to 100 jobs per request with an API key. We use the cap
# for backfill throughput; smaller batches won't gain anything and waste header.
_MAX_JOBS_PER_REQUEST = 100

# Default per-call HTTP timeout. OpenFIGI mapping is typically sub-second, but
# we allow a comfortable headroom for the per-batch round-trip including TLS.
_DEFAULT_TIMEOUT_S = 20.0

# Courtesy delay between batches when iterating > 1 batch in one call. Keeps us
# comfortably under the 25-req-per-6s rate limit even with no inter-call gap.
_INTER_BATCH_DELAY_S = 0.25


class OpenFIGIResult(BaseModel):
    """One ticker → FIGI mapping result.

    `figi_not_found` is True iff OpenFIGI returned the `{"warning": "No identifier found."}`
    response shape (HTTP 200; NOT a 404). Callers should treat this as a clean
    "no FIGI exists for that ticker" outcome, not an error — log it and move on.
    All FIGI fields are None when `figi_not_found` is True.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    """The ticker we sent (echoed back for batch-result correlation)."""

    exch_code: str
    """The exch_code we sent (echoed back; usually 'US')."""

    composite_figi: str | None = None
    """The per-jurisdiction FIGI — the one we STORE on `ticker_classifications.figi`."""

    share_class_figi: str | None = None
    """The global share-class FIGI — captured for cross-vendor reconciliation only."""

    exchange_figi: str | None = None
    """The most-granular exchange-level FIGI — captured for diagnostic only."""

    name: str | None = None
    """Issuer / instrument name as OpenFIGI sees it (cross-vendor reconciliation)."""

    security_type: str | None = None
    """OpenFIGI's security_type label (e.g. 'Common Stock', 'ETP', 'ADR')."""

    market_sector: str | None = None
    """OpenFIGI's market_sector label (e.g. 'Equity', 'Govt', 'Corp')."""

    figi_not_found: bool = False
    """True when OpenFIGI returned the explicit no-match warning."""


def _validate_figi(value: str | None) -> str | None:
    """Return value if it matches the OMG FIGI 1.2 regex, else None.

    Vendor-anomaly defense: if OpenFIGI ever returns a malformed FIGI we
    drop it rather than store garbage. Logs WARN with the bad value.
    """
    if value is None:
        return None
    if OPENFIGI_FIGI_REGEX.fullmatch(value):
        return value
    logger.warning("openfigi.malformed_figi_rejected", value=value)
    return None


class OpenFIGIAdapter:
    """Async client for OpenFIGI v3 mapping (TICKER → FIGI).

    Construct once per scheduler/parent_resolver run; reuse for multiple
    `map_tickers` calls so the underlying httpx pool is reused.

    Args:
        api_key: OpenFIGI API key. Defaults to `OPEN_FIGI_API_KEY` env var.
                 Missing key raises `DataProviderOutage` at construction (fail-fast).
        client: optional pre-built `httpx.AsyncClient` for tests (e.g. MockTransport).
        base_url: override the API base URL (env `OPENFIGI_BASE_URL`, else const).
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        base_url: str | None = None,
    ) -> None:
        key = api_key or os.environ.get("OPEN_FIGI_API_KEY")
        if not key:
            raise DataProviderOutage(
                "OpenFIGIAdapter: OPEN_FIGI_API_KEY env var is required; "
                "the adapter rate-limit class collapses without it (10x jobs/req lost)."
            )
        self._api_key = key
        self._base_url = base_url or os.environ.get("OPENFIGI_BASE_URL", OPENFIGI_BASE_URL)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_S)

    async def __aenter__(self) -> OpenFIGIAdapter:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying httpx client if we own it."""
        if self._owns_client:
            await self._client.aclose()

    @with_retry(max_attempts=3, backoff_base_sec=2.0, backoff_cap_sec=30.0)
    async def _post_mapping_batch(self, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """POST one batch of up to 100 mapping jobs.

        Decorated with `with_retry` for 429/5xx/network/timeout. Permanent
        4xx (401/403/400) raise immediately via `raise_for_status` and are
        mapped to `DataProviderOutage` at the public-method boundary.
        """
        resp = await self._client.post(
            f"{self._base_url}/v3/mapping",
            json=jobs,
            headers={
                "Content-Type": "application/json",
                "X-OPENFIGI-APIKEY": self._api_key,
            },
        )
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, list):
            raise DataProviderOutage(
                f"OpenFIGIAdapter: unexpected response shape (not a list): {type(body).__name__}"
            )
        return body

    async def map_tickers(
        self,
        tickers: list[str],
        *,
        exch_code: str = "US",
    ) -> list[OpenFIGIResult]:
        """Resolve a batch of tickers to OpenFIGI mapping results.

        Returns one `OpenFIGIResult` per input ticker, in input order.
        Tickers not found at OpenFIGI return a result with `figi_not_found=True`
        and all FIGI fields as None. The caller decides how to log /
        emit `IDENTITY_DIVERGENCE_INVESTIGATE` events.

        Raises:
            DataProviderOutage: permanent failure (auth, bad request) or
                exhausted retries on 429/5xx/network. Engines should treat
                this as "no data, no trade" for the affected tickers.
        """
        if not tickers:
            return []

        results: list[OpenFIGIResult] = []
        # Slice into ≤_MAX_JOBS_PER_REQUEST batches; insert courtesy delay between batches.
        import asyncio  # local import keeps top-of-module imports lean for static analysis
        for batch_start in range(0, len(tickers), _MAX_JOBS_PER_REQUEST):
            batch = tickers[batch_start : batch_start + _MAX_JOBS_PER_REQUEST]
            jobs = [{"idType": "TICKER", "idValue": t, "exchCode": exch_code} for t in batch]
            try:
                raw_results = await self._post_mapping_batch(jobs)
            except httpx.HTTPStatusError as e:
                # Permanent 4xx (not 429) — fail-fast, mapped to DataProviderOutage
                raise DataProviderOutage(
                    f"OpenFIGIAdapter: HTTP {e.response.status_code} from /v3/mapping "
                    f"(batch starting at {batch_start}, {len(batch)} jobs): "
                    f"{e.response.text[:200]}"
                ) from e
            except (httpx.NetworkError, httpx.TimeoutException) as e:
                raise DataProviderOutage(
                    f"OpenFIGIAdapter: network/timeout failure after retries on /v3/mapping: {e}"
                ) from e

            if len(raw_results) != len(batch):
                raise DataProviderOutage(
                    f"OpenFIGIAdapter: result-count mismatch ({len(raw_results)} != {len(batch)}); "
                    f"OpenFIGI may have changed batch-result correlation semantics."
                )

            for ticker, raw in zip(batch, raw_results, strict=True):
                results.append(_parse_one_result(ticker=ticker, exch_code=exch_code, raw=raw))

            # Courtesy delay between batches — keeps us under the 25/6s rate limit
            # even if the caller issues many batches back-to-back.
            if batch_start + _MAX_JOBS_PER_REQUEST < len(tickers):
                await asyncio.sleep(_INTER_BATCH_DELAY_S)

        return results


def _parse_one_result(
    *,
    ticker: str,
    exch_code: str,
    raw: dict[str, Any],
) -> OpenFIGIResult:
    """Convert one OpenFIGI raw result dict to an OpenFIGIResult.

    Three shapes possible per spec:
    - `{"data": [{...}, ...]}` — one or more matches; we take the first (most-relevant).
    - `{"warning": "No identifier found."}` — clean no-match outcome.
    - `{"error": "..."}` — provider-side error; we map to DataProviderOutage.
    """
    if "error" in raw:
        raise DataProviderOutage(
            f"OpenFIGIAdapter: per-job error for ticker {ticker!r}: {raw['error']}"
        )

    if "warning" in raw:
        # The canonical "no identifier found" path. Not an exception.
        logger.debug("openfigi.no_match", ticker=ticker, warning=raw.get("warning"))
        return OpenFIGIResult(ticker=ticker, exch_code=exch_code, figi_not_found=True)

    data = raw.get("data")
    if not data:
        # Unexpected: no error, no warning, no data. Treat as no-match defensively.
        logger.warning("openfigi.empty_data", ticker=ticker, raw=raw)
        return OpenFIGIResult(ticker=ticker, exch_code=exch_code, figi_not_found=True)

    first = data[0]
    return OpenFIGIResult(
        ticker=ticker,
        exch_code=exch_code,
        composite_figi=_validate_figi(first.get("compositeFIGI")),
        share_class_figi=_validate_figi(first.get("shareClassFIGI")),
        exchange_figi=_validate_figi(first.get("figi")),
        name=first.get("name"),
        security_type=first.get("securityType"),
        market_sector=first.get("marketSector"),
        figi_not_found=False,
    )
