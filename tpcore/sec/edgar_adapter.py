"""SEC EDGAR adapter — Form 4 insider transactions + 8-K material events.

Public endpoints (no API key, ``User-Agent`` mandatory per SEC's
fair-access policy at https://www.sec.gov/os/accessing-edgar-data):

* ``https://www.sec.gov/files/company_tickers.json`` — ticker → CIK map.
* ``https://data.sec.gov/submissions/CIK<10-digit>.json`` — per-CIK
  filing index (form types, accession numbers, filing dates).
* ``https://www.sec.gov/Archives/edgar/data/<cik>/<accession-clean>/
  <accession-with-dashes>.txt`` — primary doc download (Form 4 XML
  is parsed; 8-K item codes come from the submissions index, so we
  don't pull the 8-K body unless the operator opts in).

Rate limit: SEC requests ≤ 10 req/sec sustained per IP. The adapter
adds a small inter-request sleep to stay well under the cap. The
canonical retry primitive (``tpcore.outage.with_retry``) handles
transient 5xx + 429 with exponential backoff and ``Retry-After``.

Reference implementation for the standard 5-stage data adapter
pipeline (``docs/superpowers/pipelines/data_adapter_pipeline.md``).
The handler ``handle_sec_filings`` runs the CSV-first sub-protocol;
this module is the pure HTTP + parser layer.
"""
from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from xml.etree import ElementTree as ET

import httpx
import structlog

from tpcore.outage import DataProviderOutage, with_retry

logger = structlog.get_logger(__name__)

# ── Configuration constants ────────────────────────────────────────────

_PROVIDER_NAME = "sec_edgar"
_USER_AGENT_ENV = "SEC_EDGAR_USER_AGENT"
# SEC requires a real contact email in the UA. Operator must set the env
# var; missing UA → fail-fast at construction (per pipeline contract).
_DEFAULT_TIMEOUT_S = 30.0
_INTER_REQUEST_SLEEP_S = 0.12  # ≈ 8 req/sec — safely under the 10/sec cap.
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_BASE = "https://data.sec.gov"
_ARCHIVES_BASE = "https://www.sec.gov"

# Form 4 transaction-code → canonical BUY/SELL bucket. Other codes
# (M=exempt, G=gift, etc.) are skipped — the operator's downstream
# signal is sensitive to open-market buys vs sells only, not exotica.
_BUY_CODES = frozenset({"P", "A"})   # P=Purchase, A=Grant/Acquisition
_SELL_CODES = frozenset({"S", "D"})  # S=Sale, D=Disposition


def _normalize_ticker(t: str) -> str:
    return t.strip().upper()


def _cik_to_padded(cik: int | str) -> str:
    return f"{int(cik):010d}"


def _parse_filing_date(raw: str) -> date | None:
    try:
        return datetime.fromisoformat(raw[:10]).date()
    except Exception:
        return None


class SECEdgarAdapter:
    """Pulls Form 4 + 8-K filings from SEC EDGAR.

    Args:
        user_agent: Required by SEC. Defaults to ``SEC_EDGAR_USER_AGENT``
            env var. Format: ``"<App> <contact-email>"``. Raises
            ``DataProviderOutage`` if missing — fail-fast at construction.
        client: optional pre-built ``httpx.AsyncClient`` for tests.
        timeout: per-request timeout in seconds. Defaults to 30.
        inter_request_sleep_s: courtesy delay between requests.
    """

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        inter_request_sleep_s: float = _INTER_REQUEST_SLEEP_S,
    ) -> None:
        ua = user_agent or os.getenv(_USER_AGENT_ENV)
        if not ua:
            raise DataProviderOutage(
                f"{_PROVIDER_NAME} adapter requires {_USER_AGENT_ENV} env var "
                "(SEC fair-access policy mandates a real contact in the UA)"
            )
        self._user_agent = ua
        self._client = client
        self._timeout = timeout
        self._inter_sleep = inter_request_sleep_s
        self._owned_client = client is None
        self._ticker_to_cik: dict[str, int] | None = None

    async def __aenter__(self) -> SECEdgarAdapter:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": self._user_agent, "Accept-Encoding": "gzip"},
                timeout=self._timeout,
            )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Ticker → CIK mapping ──────────────────────────────────────────
    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": self._user_agent, "Accept-Encoding": "gzip"},
                timeout=self._timeout,
            )
            self._owned_client = True
        return self._client

    async def load_ticker_to_cik(self) -> dict[str, int]:
        """Fetch and cache the SEC's public ticker→CIK map.

        Cached for the lifetime of the adapter instance. Operator runs a
        new instance per ingest run, so the cache stays fresh per-run
        without going stale across runs.
        """
        if self._ticker_to_cik is not None:
            return self._ticker_to_cik
        payload = await self._fetch_raw(_TICKERS_URL)
        # SEC returns a dict keyed by row index: {"0": {"cik_str": 320193, "ticker": "AAPL", ...}, ...}
        result: dict[str, int] = {}
        items = payload.values() if isinstance(payload, dict) else payload
        for row in items:
            try:
                ticker = _normalize_ticker(str(row["ticker"]))
                cik = int(row["cik_str"])
            except (KeyError, TypeError, ValueError):
                continue
            result[ticker] = cik
        self._ticker_to_cik = result
        logger.info(f"{_PROVIDER_NAME}.ticker_map_loaded", count=len(result))
        return result

    # ── Public API: filings for one ticker ────────────────────────────
    async def get_recent_filings(
        self,
        ticker: str,
        *,
        forms: Iterable[str] = ("4", "8-K"),
        since: date | None = None,
        full_history: bool = False,
    ) -> list[dict[str, Any]]:
        """Return submission-index rows for the ticker's filings.

        Each row carries ``form``, ``filing_date``, ``accession_number``,
        ``primary_document``, and ``items`` (8-K item-codes; empty for
        Form 4). The caller decides whether to fetch + parse the Form 4
        body — see ``parse_form4_transactions``.

        By default only ``filings.recent`` (SEC's ~1000 latest) is read
        — correct + cheap for the daily/weekly incremental. With
        ``full_history=True`` the older ``filings.files`` shards whose
        date range overlaps ``since..today`` are also fetched and
        merged, so a historical backfill does not silently miss filings
        that aged out of ``recent`` for prolific filers. Default is
        unchanged behaviour (no impact on existing callers/engines).

        Raises ``DataProviderOutage`` on permanent failure (unknown
        ticker, 4xx-not-429, exhausted retries).
        """
        ticker_n = _normalize_ticker(ticker)
        ticker_map = await self.load_ticker_to_cik()
        cik = ticker_map.get(ticker_n)
        if cik is None:
            # Not a permanent outage — many universe tickers aren't in
            # SEC's company_tickers.json (foreign issuers, OTC names,
            # very small caps). Return empty; caller logs.
            logger.debug(f"{_PROVIDER_NAME}.no_cik", ticker=ticker_n)
            return []
        url = f"{_SUBMISSIONS_BASE}/submissions/CIK{_cik_to_padded(cik)}.json"
        payload = await self._fetch_raw(url)
        filings = (payload or {}).get("filings", {}) or {}
        forms_filter = {str(f).upper() for f in forms}
        results: list[dict[str, Any]] = []

        def _emit(block: dict[str, Any]) -> None:
            forms_list = block.get("form", []) or []
            dates_list = block.get("filingDate", []) or []
            acc_list = block.get("accessionNumber", []) or []
            primary_list = block.get("primaryDocument", []) or []
            items_list = block.get("items", []) or []
            for i, form in enumerate(forms_list):
                if str(form).upper() not in forms_filter:
                    continue
                fd = (_parse_filing_date(dates_list[i])
                      if i < len(dates_list) else None)
                if fd is None:
                    continue
                if since is not None and fd < since:
                    continue
                results.append({
                    "ticker": ticker_n,
                    "cik": cik,
                    "form": str(form).upper(),
                    "filing_date": fd,
                    "accession_number": (str(acc_list[i])
                                         if i < len(acc_list) else ""),
                    "primary_document": (str(primary_list[i])
                                         if i < len(primary_list) else ""),
                    "items": (str(items_list[i])
                              if i < len(items_list) else ""),
                })

        recent = filings.get("recent", {}) or {}
        if recent:
            _emit(recent)

        if full_history:
            # Older filings live in dated shards under ``filings.files``.
            # Fetch only the shards whose [filingFrom, filingTo] overlaps
            # the requested window — complete for ``since``, without
            # pulling decades of irrelevant history.
            for shard in filings.get("files", []) or []:
                name = shard.get("name")
                if not name:
                    continue
                if since is not None:
                    s_to = _parse_filing_date(str(shard.get("filingTo", "")))
                    if s_to is not None and s_to < since:
                        continue
                shard_payload = await self._fetch_raw(
                    f"{_SUBMISSIONS_BASE}/submissions/{name}"
                )
                _emit(shard_payload or {})

        return results

    # ── Form 4 XML parsing ────────────────────────────────────────────
    async def fetch_form4_xml(
        self, cik: int, accession_number: str, primary_document: str,
    ) -> str:
        """Download the Form 4 raw XML.

        The submissions API's ``primaryDocument`` field commonly points
        at the *XSL-rendered HTML* version (e.g. ``xslF345X06/form4.xml``).
        Hitting that path returns HTML the parser can't read. Strip any
        leading ``xslF345X*/`` directory prefix to hit the underlying
        raw XML in the same accession folder. Fix landed 2026-05-14
        after the historical-backfill diagnosis showed every Form 4
        was producing 0 rows because we were parsing HTML.
        """
        clean_acc = accession_number.replace("-", "")
        # SEC stores both representations side-by-side; strip the XSL
        # rendition prefix to land on raw XML.
        doc = re.sub(r"^xslF345X\d+/", "", primary_document)
        url = (
            f"{_ARCHIVES_BASE}/Archives/edgar/data/{int(cik)}/"
            f"{clean_acc}/{doc}"
        )
        client = await self._ensure_client()
        resp = await self._http_get_text(client, url)
        return resp

    @staticmethod
    def parse_form4_transactions(
        xml_text: str, ticker: str, filing_date: date,
    ) -> tuple[list[dict[str, Any]], int]:
        """Parse a Form 4 XML body into canonical transaction rows.

        Returns ``(rows, skipped_count)`` — ``rows`` is one dict per
        non-derivative open-market BUY/SELL transaction; ``skipped_count``
        is the number of lines we couldn't classify (exotic codes,
        derivatives, malformed amounts). Skipped lines never become
        rows — they don't pass the physical-truth gate.
        """
        rows: list[dict[str, Any]] = []
        skipped = 0
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return [], 1

        # Reporting owner (insider) name — Form 4 schema has it under
        # reportingOwner/reportingOwnerId/rptOwnerName.
        owner_name = "UNKNOWN"
        owner_el = root.find(".//reportingOwner/reportingOwnerId/rptOwnerName")
        if owner_el is not None and owner_el.text:
            owner_name = owner_el.text.strip()

        for tx in root.iterfind(".//nonDerivativeTable/nonDerivativeTransaction"):
            # Buy/sell direction comes from transactionCode (text directly
            # on the element, NOT wrapped in <value>). The earlier
            # ``transactionAcquiredDisposedCode/value`` XPath was wrong —
            # that element doesn't exist in Form 4 schema X0306+ (which
            # is the only schema SEC has served for years). Fix landed
            # 2026-05-14 alongside the URL fix.
            code_el = tx.find("transactionCoding/transactionCode")
            shares_el = tx.find("transactionAmounts/transactionShares/value")
            price_el = tx.find(
                "transactionAmounts/transactionPricePerShare/value"
            )

            code = (code_el.text or "").strip().upper() if code_el is not None and code_el.text else ""
            if code in _BUY_CODES:
                tx_type = "BUY"
            elif code in _SELL_CODES:
                tx_type = "SELL"
            else:
                skipped += 1
                continue

            try:
                shares = int(Decimal(shares_el.text or "0"))  # type: ignore[union-attr]
            except (AttributeError, ValueError, Exception):
                skipped += 1
                continue
            if shares <= 0:
                skipped += 1
                continue

            try:
                price = Decimal(price_el.text or "0") if price_el is not None and price_el.text else Decimal(0)
            except Exception:
                price = Decimal(0)
            if price < 0:
                price = Decimal(0)
            value = (price * Decimal(shares)).quantize(Decimal("0.01"))

            rows.append({
                "ticker": ticker,
                "filing_date": filing_date,
                "insider_name": owner_name,
                "transaction_type": tx_type,
                "shares": shares,
                "price": price,
                "value": value,
            })
        return rows, skipped

    # ── 8-K item parsing ──────────────────────────────────────────────
    @staticmethod
    def parse_8k_items(items_str: str) -> list[str]:
        """Split the comma-separated ``items`` field from a submissions
        row into individual canonical 8-K item codes (e.g., ``"2.02, 9.01"``
        → ``["2.02", "9.01"]``). Returns ``["OTHER"]`` for empty/unknown.
        """
        if not items_str or not items_str.strip():
            return ["OTHER"]
        out: list[str] = []
        for raw in re.split(r"[,\s]+", items_str):
            r = raw.strip().rstrip(".")
            if not r:
                continue
            # Normalize: SEC sometimes ships "Item2.02" or "Item 2.02".
            r = r.replace("Item", "").strip()
            if r:
                out.append(r)
        return out or ["OTHER"]

    # ── Internal: HTTP layer ──────────────────────────────────────────
    @with_retry(max_attempts=4, backoff_base_sec=2.0, backoff_cap_sec=60.0)
    async def _fetch_raw(self, url: str) -> Any:
        client = await self._ensure_client()
        resp = await client.get(url)
        if resp.status_code == 200:
            await asyncio.sleep(self._inter_sleep)
            return resp.json()
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            raise httpx.HTTPStatusError(
                f"{_PROVIDER_NAME} {url} → {resp.status_code}",
                request=resp.request,
                response=resp,
            )
        # 4xx-not-429 → permanent. Raise DataProviderOutage so the
        # handler can record a clean error reason and skip the ticker.
        raise DataProviderOutage(
            f"{_PROVIDER_NAME} {url} returned {resp.status_code}"
        )

    @with_retry(max_attempts=4, backoff_base_sec=2.0, backoff_cap_sec=60.0)
    async def _http_get_text(
        self, client: httpx.AsyncClient, url: str,
    ) -> str:
        resp = await client.get(url)
        if resp.status_code == 200:
            await asyncio.sleep(self._inter_sleep)
            return resp.text
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            raise httpx.HTTPStatusError(
                f"{_PROVIDER_NAME} {url} → {resp.status_code}",
                request=resp.request,
                response=resp,
            )
        raise DataProviderOutage(
            f"{_PROVIDER_NAME} {url} returned {resp.status_code}"
        )


__all__ = ["SECEdgarAdapter"]
