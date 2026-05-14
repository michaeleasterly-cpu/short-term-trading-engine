"""FMP quarterly fundamentals adapter.

Pulls income, cash-flow, and balance-sheet quarterly statements from
FMP's ``/stable/`` API and merges them into a single per-period dict.
Provides ``get_quarterly_fundamentals(symbol, as_of_date)`` for
point-in-time use — when ``as_of_date`` is given, the adapter returns
the latest filing whose ``filingDate <= as_of_date``, plus an optional
history slice for trend computations.

Engines treat any persistent failure as ``DataProviderOutage`` — "no
data, no trade." Tenacity-backed retries handle transient blips. A
small in-process cache dedups within a single scheduler run; the
durable cache is ``platform.fundamentals_quarterly`` (not yet built).
"""
from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import httpx
import structlog

from tpcore.outage import DataProviderOutage, with_retry

logger = structlog.get_logger(__name__)

FMP_BASE_URL = "https://financialmodelingprep.com/stable"

# Field mappings — keep in lock-step with the response shape probed against
# FMP's /stable/ surface. Brief used legacy v3 names (e.g. `currentAssets`);
# /stable/ uses `totalCurrentAssets`. We normalize on the way out.
_INCOME_FIELDS = ("revenue", "netIncome", "filingDate", "date", "period")
_CASH_FLOW_FIELDS = (
    "freeCashFlow", "operatingCashFlow", "capitalExpenditure",
    "filingDate", "date", "period",
)
_BALANCE_FIELDS = (
    "totalAssets", "totalLiabilities",
    "totalCurrentAssets", "totalCurrentLiabilities",
    "netReceivables", "cashAndCashEquivalents",
    "filingDate", "date", "period",
)

DEFAULT_LIMIT = 40  # ~10 years of quarterly data. Free tier silently caps at 5; Starter+ honors larger values.
DEFAULT_TIMEOUT_S = 20.0


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _parse_filing_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except Exception:
        return None


class FMPFundamentalsAdapter:
    """Fetches and merges FMP quarterly statements.

    Args:
        api_key: FMP API key (defaults to ``FMP_API_KEY`` env var).
        client: optional pre-built ``httpx.AsyncClient`` for tests.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key or os.getenv("FMP_API_KEY")
        if not self._api_key:
            raise DataProviderOutage(
                "FMP_API_KEY not set in environment — fundamentals adapter cannot start"
            )
        self._client = client
        self._owned_client = client is None
        # Cache: (symbol, as_of_iso) → fundamentals dict.
        self._cache: dict[tuple[str, str | None], dict] = {}

    async def __aenter__(self) -> FMPFundamentalsAdapter:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owned_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def aclose(self) -> None:
        await self.__aexit__(None, None, None)

    async def get_quarterly_fundamentals(
        self,
        symbol: str,
        as_of_date: date | None = None,
        *,
        limit: int = DEFAULT_LIMIT,
    ) -> dict:
        """Return a normalized fundamentals dict for ``symbol``.

        When ``as_of_date`` is set, only periods with
        ``filingDate <= as_of_date`` are considered (PIT-safe). The
        returned dict has the latest qualifying period at the top level
        plus a ``history`` list of preceding periods for trend math.
        """
        cache_key = (symbol.upper(), as_of_date.isoformat() if as_of_date else None)
        if cache_key in self._cache:
            return self._cache[cache_key]

        income = await self._fetch("income-statement", symbol, limit)
        cash = await self._fetch("cash-flow-statement", symbol, limit)
        balance = await self._fetch("balance-sheet-statement", symbol, limit)

        merged = self._merge(income, cash, balance, as_of_date=as_of_date)
        if not merged:
            raise DataProviderOutage(
                f"FMP returned no usable fundamentals for {symbol} as_of={as_of_date}"
            )

        latest = merged[0]
        history = merged[1:]
        out = dict(latest)
        out["history"] = history
        out["symbol"] = symbol.upper()
        self._cache[cache_key] = out
        return out

    @with_retry(max_attempts=3, backoff_base_sec=1.0, backoff_cap_sec=10.0)
    async def _fetch_raw(self, endpoint: str, symbol: str, limit: int) -> list[dict]:
        """Single FMP fetch with retry baked in (429/5xx/network/timeout).

        Refactored 2026-05-14 to drop the local ``tenacity.AsyncRetrying``
        scaffolding in favor of the platform's shared
        ``tpcore.outage.with_retry`` decorator. Same retry semantics
        (3 attempts, exponential backoff, 429/5xx retryable) but
        consistent with every other external API call on the platform.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S)
            self._owned_client = True
        url = f"{FMP_BASE_URL}/{endpoint}"
        params = {
            "symbol": symbol,
            "period": "quarter",
            "limit": str(limit),
            "apikey": self._api_key,
        }
        resp = await self._client.get(url, params=params)
        if resp.status_code == 200:
            return resp.json()
        # 429 / 5xx → raise so the @with_retry decorator retries.
        # 4xx-not-429 → DataProviderOutage immediately (permanent).
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            raise httpx.HTTPStatusError(
                f"FMP {endpoint} → {resp.status_code}",
                request=resp.request,
                response=resp,
            )
        raise DataProviderOutage(
            f"FMP {endpoint} {symbol} returned {resp.status_code}: "
            f"{resp.text[:200]}"
        )

    async def _fetch(self, endpoint: str, symbol: str, limit: int) -> list[dict]:
        try:
            return await self._fetch_raw(endpoint, symbol, limit)
        except DataProviderOutage:
            # Already classified — pass through.
            raise
        except httpx.HTTPError as exc:
            # Retry exhausted (5xx, network, timeout). Map to outage.
            raise DataProviderOutage(
                f"FMP {endpoint} {symbol} unreachable after retries: {exc}"
            ) from exc

    def _merge(
        self,
        income: list[dict],
        cash: list[dict],
        balance: list[dict],
        *,
        as_of_date: date | None,
    ) -> list[dict]:
        """Inner-join the three statements on ``date`` (the period end).

        FMP returns lists ordered most-recent-first. We index by ``date``
        to handle the rare case where the three statements come back
        slightly out of order, then re-sort descending by ``filingDate``.
        """
        by_date_income = {row.get("date"): row for row in income}
        by_date_cash = {row.get("date"): row for row in cash}
        by_date_balance = {row.get("date"): row for row in balance}
        common_dates = set(by_date_income) & set(by_date_cash) & set(by_date_balance)

        merged: list[dict] = []
        for d in common_dates:
            inc = by_date_income[d]
            cf = by_date_cash[d]
            bs = by_date_balance[d]
            filing_date = _parse_filing_date(
                inc.get("filingDate") or cf.get("filingDate") or bs.get("filingDate")
            )
            if as_of_date is not None and (filing_date is None or filing_date > as_of_date):
                continue
            merged.append(
                {
                    "period": inc.get("period") or cf.get("period") or bs.get("period"),
                    "period_end_date": _parse_filing_date(d) or d,
                    "filing_date": filing_date,
                    "net_income": _to_decimal(inc.get("netIncome")),
                    "revenue": _to_decimal(inc.get("revenue")),
                    "shares_outstanding": _to_decimal(
                        inc.get("weightedAverageShsOutDil") or inc.get("weightedAverageShsOut")
                    ),
                    "fcf": _to_decimal(cf.get("freeCashFlow")),
                    "operating_cash_flow": _to_decimal(cf.get("operatingCashFlow")),
                    "capex": _to_decimal(cf.get("capitalExpenditure")),
                    "total_assets": _to_decimal(bs.get("totalAssets")),
                    "total_liabilities": _to_decimal(bs.get("totalLiabilities")),
                    "current_assets": _to_decimal(bs.get("totalCurrentAssets")),
                    "current_liabilities": _to_decimal(bs.get("totalCurrentLiabilities")),
                    "receivables": _to_decimal(
                        bs.get("netReceivables") or bs.get("accountsReceivables")
                    ),
                    "cash_and_equivalents": _to_decimal(bs.get("cashAndCashEquivalents")),
                }
            )
        merged.sort(key=lambda r: (r["filing_date"] or date.min), reverse=True)
        return merged


__all__ = ["FMPFundamentalsAdapter"]
