"""SEC EDGAR XBRL companyfacts adapter — authoritative US-filer
fundamentals source, used as a CASCADE FALLBACK when FMP's 3-endpoint
merge leaves period gaps.

Endpoint: ``https://data.sec.gov/api/xbrl/companyfacts/CIK<10-digit>.json``
No API key required. ``User-Agent`` mandatory (SEC fair-use policy);
sourced from the ``SEC_EDGAR_USER_AGENT`` env var.

Per memory ``feedback_sec_authoritative_fmp_fallback_non_us``: SEC is
the US-filer authoritative source. The fundamentals_quarterly check
detects FMP coverage gaps for pre-IPO predecessor periods, recent IPOs,
and balance-sheet-sparse filers — periods FMP genuinely doesn't have
but the SEC XBRL companyfacts does (every 10-Q a filer ever submitted).

XBRL fact mapping (US-GAAP standard concepts):

  ============================================================  ================================
  XBRL concept                                                  target column
  ============================================================  ================================
  Revenues / RevenueFromContractWithCustomerExcludingAssessedTax revenue
  NetIncomeLoss                                                  net_income
  Assets                                                         total_assets
  Liabilities                                                    total_liabilities
  AssetsCurrent                                                  current_assets
  LiabilitiesCurrent                                             current_liabilities
  CashAndCashEquivalentsAtCarryingValue                          cash_and_equivalents
  AccountsReceivableNetCurrent                                   receivables
  NetCashProvidedByUsedInOperatingActivities                     operating_cash_flow
  PaymentsToAcquirePropertyPlantAndEquipment                     capex (sign-flipped to negative outflow)
  CommonStockSharesOutstanding /
    EntityCommonStockSharesOutstanding (dei)                     shares_outstanding
  ============================================================  ================================

``fcf`` is derived: ``operating_cash_flow - capex_raw`` (capex_raw is
the positive SEC value before sign-flipping; fcf math mirrors the FMP
adapter's convention).
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import structlog

from tpcore.outage import DataProviderOutage

if TYPE_CHECKING:  # pragma: no cover
    pass


logger = structlog.get_logger(__name__)


SEC_DATA_BASE_URL = "https://data.sec.gov"
SEC_DATA_BASE_URL_ENV = "SEC_DATA_BASE_URL"
SEC_USER_AGENT_ENV = "SEC_EDGAR_USER_AGENT"
_DEFAULT_USER_AGENT_FALLBACK = "STE/1.0 ops@short-term-trading-engine.local"

# Per SEC fair-use guidance: ~10 req/sec unauthenticated. Adapter inter-
# call sleep is the caller's responsibility (the handler enforces it).
_TIMEOUT_S = 30.0

# Fact-name priority lists. SEC reports vary by filer — the first key
# with a value for a given period_end wins.
REVENUE_KEYS: tuple[str, ...] = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
)
SHARES_KEYS: tuple[str, ...] = (
    "CommonStockSharesOutstanding",
    "EntityCommonStockSharesOutstanding",
)


class SECCompanyFactsAdapter:
    """HTTP adapter for ``data.sec.gov/api/xbrl/companyfacts``.

    Usage::

        async with SECCompanyFactsAdapter() as adapter:
            facts = await adapter.get_companyfacts(cik="0000320193")
            period = adapter.extract_period(facts, date(2024, 3, 31))
    """

    def __init__(self, base_url: str | None = None, user_agent: str | None = None) -> None:
        self._base_url = (base_url or os.environ.get(SEC_DATA_BASE_URL_ENV) or SEC_DATA_BASE_URL).rstrip("/")
        ua = user_agent or os.environ.get(SEC_USER_AGENT_ENV)
        if not ua:
            # Fail loud — SEC drops requests without a User-Agent.
            raise DataProviderOutage(
                f"{SEC_USER_AGENT_ENV} not set in environment — SEC EDGAR "
                "requires a contact User-Agent per their fair-use policy. "
                "Set to 'name email' (e.g. 'STE/1.0 you@example.com')."
            )
        self._user_agent = ua
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> SECCompanyFactsAdapter:
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"User-Agent": self._user_agent, "Accept": "application/json"},
            timeout=_TIMEOUT_S,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get_companyfacts(self, cik: str) -> dict | None:
        """Fetch the full XBRL companyfacts JSON for a CIK.

        Returns ``None`` when SEC reports 404 (CIK has no XBRL filings —
        common for SPACs / pre-IPO shells). Raises ``DataProviderOutage``
        on other HTTP errors so the caller can record + escalate.
        """
        if self._client is None:
            raise RuntimeError("SECCompanyFactsAdapter must be used as a context manager")
        cik_padded = cik.lstrip("0").zfill(10)
        url = f"/api/xbrl/companyfacts/CIK{cik_padded}.json"
        try:
            resp = await self._client.get(url)
        except httpx.RequestError as exc:
            raise DataProviderOutage(
                f"SEC companyfacts CIK={cik}: network error {type(exc).__name__}: {exc}"
            ) from exc
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise DataProviderOutage(
                f"SEC companyfacts CIK={cik} returned {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()

    @staticmethod
    def extract_period(facts: dict, period_end: date) -> dict | None:
        """Extract one period's normalized financial fields from a
        companyfacts payload. Returns ``None`` if the period has no
        usable financial signal (no revenue / income / assets / OCF).

        Mirrors the schema columns on ``platform.fundamentals_quarterly``
        so the caller can upsert directly via the existing cache contract.

        SEC companyfacts JSON shape (verified against live API
        2026-05-26 for CIK 1726711 / ADTX): the top-level dict has
        ``cik`` + ``entityName`` + ``facts``, and the namespace dicts
        (``us-gaap``, ``dei``, ``srt``, ``ffd``) live UNDER ``facts``,
        not at the top level. This caused a silent always-None bug in
        the first impl that called ``facts.get("us-gaap")`` directly.
        """
        ns = facts.get("facts") or {}
        us_gaap = ns.get("us-gaap") or {}
        dei = ns.get("dei") or {}

        def _val(scope: dict, key: str) -> Decimal | None:
            entry = scope.get(key)
            if not entry:
                return None
            units = entry.get("units") or {}
            for _unit, rows in units.items():
                for r in rows:
                    if r.get("end") == period_end.isoformat() and r.get("val") is not None:
                        return Decimal(str(r["val"]))
            return None

        def _val_any(scope: dict, keys: tuple[str, ...]) -> Decimal | None:
            for k in keys:
                v = _val(scope, k)
                if v is not None:
                    return v
            return None

        revenue = _val_any(us_gaap, REVENUE_KEYS)
        net_income = _val(us_gaap, "NetIncomeLoss")
        assets = _val(us_gaap, "Assets")
        liabilities = _val(us_gaap, "Liabilities")
        current_assets = _val(us_gaap, "AssetsCurrent")
        current_liab = _val(us_gaap, "LiabilitiesCurrent")
        cash = _val(us_gaap, "CashAndCashEquivalentsAtCarryingValue")
        receivables = _val(us_gaap, "AccountsReceivableNetCurrent")
        ocf = _val(us_gaap, "NetCashProvidedByUsedInOperatingActivities")
        capex_raw = _val(us_gaap, "PaymentsToAcquirePropertyPlantAndEquipment")
        capex = (-capex_raw) if capex_raw is not None else None
        fcf = (ocf - capex_raw) if (ocf is not None and capex_raw is not None) else None
        shares = _val_any(us_gaap, SHARES_KEYS) or _val_any(dei, SHARES_KEYS)

        # Need at least one financial signal to consider this a real period.
        if not any([revenue, net_income, assets, liabilities, ocf]):
            return None

        return {
            "period_end_date": period_end,
            "revenue": revenue,
            "net_income": net_income,
            "total_assets": assets,
            "total_liabilities": liabilities,
            "current_assets": current_assets,
            "current_liabilities": current_liab,
            "cash_and_equivalents": cash,
            "receivables": receivables,
            "operating_cash_flow": ocf,
            "capex": capex,
            "fcf": fcf,
            "shares_outstanding": shares,
        }


__all__ = [
    "SECCompanyFactsAdapter",
    "SEC_DATA_BASE_URL",
    "SEC_DATA_BASE_URL_ENV",
    "SEC_USER_AGENT_ENV",
    "REVENUE_KEYS",
    "SHARES_KEYS",
]
