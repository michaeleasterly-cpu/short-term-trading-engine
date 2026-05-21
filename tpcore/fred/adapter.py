"""FRED macro-indicators adapter.

Pulls daily/weekly/monthly observations for the macro series listed
in :data:`INDICATOR_SERIES` from the St. Louis Fed FRED API:

* ``sahm_rule``            — SAHMREALTIME (monthly recession indicator)
* ``industrial_production`` — INDPRO (monthly PMI proxy)
* ``initial_claims``       — IC4WSA (weekly 4-wk MA jobless claims)
* ``yield_curve``          — T10Y2Y (daily 10y-2y Treasury spread)
* ``credit_spread``        — BAA10Y (Moody's Seasoned Baa Corporate Bond
                              Yield relative to the 10-Year Treasury,
                              daily — credit stress proxy)
* ``hy_spread``            — BAMLH0A0HYM2 (daily HY OAS; FRED-rolling
                              tail + recovered pre-2023 history)
* ``vix``                  — VIXCLS (daily CBOE Volatility Index close)
* ``cfnai_ma3``            — CFNAIMA3 (monthly Chicago Fed National
                              Activity Index, 3-month MA — Sentinel
                              Bear Score band anchor, added 2026-05-20)
* ``phci_<state>`` × 50    — {XX}PHCI (monthly Coincident Economic
                              Activity Index per US state, Phila Fed;
                              1979→present; substrate for the derived
                              ``sos_state_diffusion`` series consumed
                              by the Sentinel graduated Bear Score Lab
                              candidate, added 2026-05-21)

**2026-05-15 — BAA10Y replaces BAMLH0A0HYM2.** FRED permanently truncated
the HY OAS series (``BAMLH0A0HYM2``) to a rolling 3-year window starting
April 2026; the full pre-2023 history is no longer accessible through
any free source. BAA10Y is a free FRED series with full history back to
1996, strong correlation with the HY OAS in crises, and no truncation.
The historical ``hy_spread`` rows in ``platform.macro_indicators`` are
retained for audit but no longer refreshed.

FRED API docs: https://fred.stlouisfed.org/docs/api/fred/
Rate limit: 120 requests per minute (we pull 5 series → 5 calls per
run — well under the limit; courtesy delay is symbolic).

Reference implementation: ``tpcore.sec.SECEdgarAdapter``. Same shape:
fail-fast at construction on missing API key, ``@with_retry`` on the
HTTP layer, ``DataProviderOutage`` mapping at the public-method
boundary, structured logging.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import structlog

from tpcore.outage import DataProviderOutage, with_retry

logger = structlog.get_logger(__name__)

# ── Configuration constants ────────────────────────────────────────────

_PROVIDER_NAME = "fred"
_API_KEY_ENV = "FRED_API_KEY"
_BASE_URL = "https://api.stlouisfed.org/fred"
_DEFAULT_TIMEOUT_S = 30.0
_INTER_REQUEST_SLEEP_S = 0.5  # well under FRED's 120/min courtesy budget


INDICATOR_SERIES: tuple[tuple[str, str], ...] = (
    ("sahm_rule",            "SAHMREALTIME"),
    ("industrial_production", "INDPRO"),
    ("initial_claims",       "IC4WSA"),
    ("yield_curve",          "T10Y2Y"),
    ("credit_spread",        "BAA10Y"),
    # hy_spread re-activated 2026-05-16: the full pre-truncation history
    # was recovered (eco-archive + Scribd gap, validated 772/772 exact)
    # and is contiguous 1996→2026 in macro_indicators. FRED still serves
    # the rolling ~3yr window for BAMLH0A0HYM2, so keeping it here lets
    # the weekly stage keep the recent tail fresh going forward
    # (idempotent ON CONFLICT — never touches the recovered history).
    # BAA10Y stays the Sentinel Bear-Score signal; the HY→Sentinel
    # scoring switch is a separate, deferred, backtest-gated decision.
    ("hy_spread",            "BAMLH0A0HYM2"),
    # VIX close (CBOE Volatility Index) — added 2026-05-16 for the
    # Fear & Greed volatility component. FRED VIXCLS has full daily
    # history from 1990-01-02; no new provider (FRED is existing).
    ("vix",                  "VIXCLS"),
    # Chicago Fed National Activity Index, 3-month moving average —
    # added 2026-05-20 to unblock the Sentinel graduated Bear Score Lab
    # candidate, which uses a ``CFNAI ≤ -0.70`` band anchor. CFNAIMA3
    # publishes MONTHLY (FRED release calendar: monthly, around the 4th
    # week of the following month). No new provider (FRED existing).
    ("cfnai_ma3",            "CFNAIMA3"),
    # ── Philadelphia Fed state coincident indices — 50 USPS states,
    # monthly, 1979→present. Substrate for the derived
    # ``sos_state_diffusion`` series (Crone/Clayton-Matthews 2005
    # 3-month span) consumed by the Sentinel graduated Bear Score Lab
    # candidate. Live-probed 2026-05-21: all 50 series valid, frequency
    # Monthly, observation_start 1979-01-01 (TX 1979-04-01). No new
    # provider (FRED existing); license-free.
    ("phci_al", "ALPHCI"), ("phci_ak", "AKPHCI"), ("phci_az", "AZPHCI"),
    ("phci_ar", "ARPHCI"), ("phci_ca", "CAPHCI"), ("phci_co", "COPHCI"),
    ("phci_ct", "CTPHCI"), ("phci_de", "DEPHCI"), ("phci_fl", "FLPHCI"),
    ("phci_ga", "GAPHCI"), ("phci_hi", "HIPHCI"), ("phci_id", "IDPHCI"),
    ("phci_il", "ILPHCI"), ("phci_in", "INPHCI"), ("phci_ia", "IAPHCI"),
    ("phci_ks", "KSPHCI"), ("phci_ky", "KYPHCI"), ("phci_la", "LAPHCI"),
    ("phci_me", "MEPHCI"), ("phci_md", "MDPHCI"), ("phci_ma", "MAPHCI"),
    ("phci_mi", "MIPHCI"), ("phci_mn", "MNPHCI"), ("phci_ms", "MSPHCI"),
    ("phci_mo", "MOPHCI"), ("phci_mt", "MTPHCI"), ("phci_ne", "NEPHCI"),
    ("phci_nv", "NVPHCI"), ("phci_nh", "NHPHCI"), ("phci_nj", "NJPHCI"),
    ("phci_nm", "NMPHCI"), ("phci_ny", "NYPHCI"), ("phci_nc", "NCPHCI"),
    ("phci_nd", "NDPHCI"), ("phci_oh", "OHPHCI"), ("phci_ok", "OKPHCI"),
    ("phci_or", "ORPHCI"), ("phci_pa", "PAPHCI"), ("phci_ri", "RIPHCI"),
    ("phci_sc", "SCPHCI"), ("phci_sd", "SDPHCI"), ("phci_tn", "TNPHCI"),
    ("phci_tx", "TXPHCI"), ("phci_ut", "UTPHCI"), ("phci_vt", "VTPHCI"),
    ("phci_va", "VAPHCI"), ("phci_wa", "WAPHCI"), ("phci_wv", "WVPHCI"),
    ("phci_wi", "WIPHCI"), ("phci_wy", "WYPHCI"),
)
"""(canonical_name, FRED series_id) pairs — the platform's vocabulary
on the left, FRED's identifier on the right. Adding a new indicator
means appending one tuple here plus a glossary entry."""


def _parse_observation_date(raw: str) -> date | None:
    try:
        return datetime.fromisoformat(raw[:10]).date()
    except Exception:
        return None


def _parse_value(raw: Any) -> Decimal | None:
    """FRED encodes missing values as ``"."``; reject those upstream of
    the DB CHECK constraint."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s == ".":
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


class FREDAdapter:
    """Pulls macro time-series from FRED.

    Args:
        api_key: FRED API key. Defaults to ``FRED_API_KEY`` env var.
            Raises ``DataProviderOutage`` at construction if missing —
            fail-fast per the adapter readiness checklist.
        client: optional pre-built ``httpx.AsyncClient`` for tests.
        timeout: per-request timeout in seconds. Defaults to 30.
        inter_request_sleep_s: courtesy delay between requests.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        inter_request_sleep_s: float = _INTER_REQUEST_SLEEP_S,
    ) -> None:
        key = api_key or os.getenv(_API_KEY_ENV)
        if not key:
            raise DataProviderOutage(
                f"{_PROVIDER_NAME} adapter requires {_API_KEY_ENV} env var "
                "(free signup at https://fred.stlouisfed.org/docs/api/api_key.html)"
            )
        self._api_key = key
        self._client = client
        self._timeout = timeout
        self._inter_sleep = inter_request_sleep_s
        self._owned_client = client is None

    async def __aenter__(self) -> FREDAdapter:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=_BASE_URL,
                timeout=self._timeout,
            )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=_BASE_URL,
                timeout=self._timeout,
            )
            self._owned_client = True
        return self._client

    # ── Public API ─────────────────────────────────────────────────────
    async def get_observations(
        self,
        series_id: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch observations for a single FRED series.

        Returns a list of ``{"date": date, "value": Decimal | None}``.
        Missing observations (FRED's ``.``) are filtered out before
        return — the loader doesn't need to repeat the check.

        Raises ``DataProviderOutage`` on permanent failure
        (4xx-not-429, exhausted retries).
        """
        params: dict[str, Any] = {
            "series_id": series_id,
            "api_key": self._api_key,
            "file_type": "json",
        }
        if start is not None:
            params["observation_start"] = start.isoformat()
        if end is not None:
            params["observation_end"] = end.isoformat()
        try:
            payload = await self._fetch_raw("/series/observations", params)
        except DataProviderOutage:
            raise
        except httpx.HTTPError as exc:
            raise DataProviderOutage(
                f"{_PROVIDER_NAME} get_observations({series_id}) unreachable: {exc}"
            ) from exc

        raw_obs = (payload or {}).get("observations", []) or []
        rows: list[dict[str, Any]] = []
        for o in raw_obs:
            d = _parse_observation_date(str(o.get("date", "")))
            v = _parse_value(o.get("value"))
            if d is None or v is None:
                continue
            rows.append({"date": d, "value": v})
        logger.info(
            f"{_PROVIDER_NAME}.observations_fetched",
            series_id=series_id,
            count_total=len(raw_obs),
            count_valid=len(rows),
        )
        return rows

    async def latest_published(self, series_id: str) -> date | None:
        """Cheap publication-availability probe (#165 facet 4): GET
        ``/fred/series?series_id=X`` and read ``observation_end`` — the
        date of FRED's latest observation for that series — WITHOUT
        downloading any actual observations. Lets the self-heal
        orchestrator distinguish "we are stale (our defect → heal)"
        from "FRED simply hasn't published a newer observation yet
        (vendor-late → quiet, no churn)" per the no-lazy-vendor-blame
        rule.

        Returns ``None`` if the response is malformed or the probe
        fails — caller falls back to the strict (assume-behind)
        behaviour, never silently green.

        Per-series rather than the AAII single-HEAD pattern because
        FRED is a multi-series feed (one ``observation_end`` per
        series). The feed-level probe in
        ``tpcore.feeds.publication`` composes per-series answers into
        a conservative "feed has nothing newer" verdict (MIN across
        series).
        """
        params: dict[str, Any] = {
            "series_id": series_id,
            "api_key": self._api_key,
            "file_type": "json",
        }
        try:
            payload = await self._fetch_raw("/series", params)
        except (DataProviderOutage, httpx.HTTPError):
            return None
        seriess = (payload or {}).get("seriess", []) or []
        if not seriess:
            return None
        raw_end = seriess[0].get("observation_end")
        if not raw_end:
            return None
        return _parse_observation_date(str(raw_end))

    async def get_all_indicators(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Fetch observations for every indicator in :data:`INDICATOR_SERIES`.

        Returns ``{canonical_name: [{date, value}, ...]}``. Inter-series
        courtesy delay applied between calls (well under FRED's 120/min
        cap). Failures on a single series log a warning and continue —
        a partial result is more useful than nothing.
        """
        out: dict[str, list[dict[str, Any]]] = {}
        for name, series_id in INDICATOR_SERIES:
            try:
                out[name] = await self.get_observations(
                    series_id, start=start, end=end,
                )
            except DataProviderOutage as exc:
                logger.warning(
                    f"{_PROVIDER_NAME}.series_failed",
                    series_id=series_id, name=name, error=str(exc),
                )
                out[name] = []
            await asyncio.sleep(self._inter_sleep)
        return out

    # ── Internal: HTTP layer ──────────────────────────────────────────
    @with_retry(max_attempts=4, backoff_base_sec=1.0, backoff_cap_sec=30.0)
    async def _fetch_raw(self, path: str, params: dict[str, Any]) -> Any:
        client = await self._ensure_client()
        resp = await client.get(path, params=params)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            raise httpx.HTTPStatusError(
                f"{_PROVIDER_NAME} {path} → {resp.status_code}",
                request=resp.request,
                response=resp,
            )
        # 4xx-not-429 → permanent. Raise DataProviderOutage with the
        # provider's error message so the operator can diagnose
        # (invalid key, bad series_id, etc.).
        raise DataProviderOutage(
            f"{_PROVIDER_NAME} {path} returned {resp.status_code}: "
            f"{resp.text[:200]}"
        )


__all__ = ["FREDAdapter", "INDICATOR_SERIES"]
