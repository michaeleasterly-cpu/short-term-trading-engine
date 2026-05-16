"""Adapter for the AAII Sentiment Survey (no auth, anti-bot-fragile).

Follows ``tpcore/templates/adapter_template.py``: ``@with_retry`` for
HTTP, ``structlog`` only, raw ``httpx.HTTPError`` mapped to
``DataProviderOutage`` at the boundary. No API key.

The American Association of Individual Investors has run a weekly
survey since 1987 (% of individual investors bullish / neutral /
bearish on the next 6 months). The full history is a single legacy
OLE2 ``.xls`` workbook served at
``https://www.aaii.com/files/surveys/sentiment.xls``.

Verified 2026-05-16: a plain request returns a 403 anti-bot block
page; a browser-shaped request (Chrome UA + Accept + Accept-Language
+ Referer) returns the real 1.1 MB workbook (HTTP 200, last saved
2026-05-14 — actively maintained). This is the same no-auth
anti-scrape posture as IBorrowDesk: a 403 is permanent per the
canonical ``with_retry`` and surfaces as ``DataProviderOutage``.

Workbook layout (verified): sheet ``SENTIMENT``; header in row index
3 (``Date | Bullish | Neutral | Bearish | Total | …``); data from
row 7; column 0 is an Excel date serial; columns 1/2/3 are the
bullish/neutral/bearish proportions as fractions in ``[0, 1]``
(× 100 → percent; each weekly row sums to ~100). Rows after the last
weekly observation are ``Count 'YY`` footer rows whose column 0 is a
string, not a date serial — the parser stops counting them as data.
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import structlog
import xlrd
from pydantic import BaseModel, ConfigDict
from xlrd.xldate import XLDateError, xldate_as_datetime

from tpcore.outage import DataProviderOutage, with_retry

logger = structlog.get_logger(__name__)

DEFAULT_TIMEOUT_S = 60.0  # 1.1 MB workbook over an anti-bot CDN
_PROVIDER_NAME = "aaii"
_BASE_URL_ENV = "AAII_BASE_URL"
_DEFAULT_BASE_URL = "https://www.aaii.com"
_SURVEY_PATH = "/files/surveys/sentiment.xls"
_SHEET_NAME = "SENTIMENT"
# Excel 1900-system serial bounds: 1987-01-01 ≈ 31778, far future cap
# keeps stray numeric cells (counts, ratios) out of the date column.
_MIN_SERIAL = 30000.0   # < 1982 — earlier than the survey can exist
_MAX_SERIAL = 80000.0   # ≈ year 2119 — well past any real row
_SUM_TOL = Decimal("1.5")  # weekly bull+neu+bear must be ~100

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_BROWSER_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.aaii.com/sentimentsurvey",
}


class AAIISentimentRecord(BaseModel):
    """One weekly AAII survey observation (canonical shape)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    date: date
    bullish_pct: Decimal
    bearish_pct: Decimal
    neutral_pct: Decimal


def _to_pct(frac: Any) -> Decimal:
    # Source stores proportions in [0, 1]; persist as percent, 2 dp.
    return (Decimal(str(frac)) * Decimal("100")).quantize(Decimal("0.01"))


def parse_sentiment_workbook(content: bytes) -> list[AAIISentimentRecord]:
    """Parse the AAII ``.xls`` bytes into weekly records.

    Raises ``DataProviderOutage("...malformed...")`` if the bytes are
    not a readable workbook, the ``SENTIMENT`` sheet is absent, or no
    well-formed weekly row can be extracted (an empty/garbage download
    must fail loudly, never silently persist nothing).
    """
    try:
        wb = xlrd.open_workbook(file_contents=content)
    except Exception as exc:  # xlrd raises a grab-bag of errors
        raise DataProviderOutage(
            f"{_PROVIDER_NAME} malformed workbook: {exc}"
        ) from exc

    try:
        sheet = wb.sheet_by_name(_SHEET_NAME)
    except Exception:
        if wb.nsheets == 0:
            raise DataProviderOutage(
                f"{_PROVIDER_NAME} malformed workbook: no sheets"
            ) from None
        sheet = wb.sheet_by_index(0)

    records: list[AAIISentimentRecord] = []
    for r in range(sheet.nrows):
        v0 = sheet.cell_value(r, 0)
        # Data rows have an Excel date serial in column 0. Header
        # (string labels), blank rows, and the trailing "Count 'YY"
        # footer rows fail this and are skipped — not malformed.
        if not isinstance(v0, (int, float)) or isinstance(v0, bool):
            continue
        if not (_MIN_SERIAL <= float(v0) <= _MAX_SERIAL):
            continue
        bull, neu, bear = (
            sheet.cell_value(r, 1),
            sheet.cell_value(r, 2),
            sheet.cell_value(r, 3),
        )
        if not all(isinstance(x, (int, float)) for x in (bull, neu, bear)):
            continue  # date present but components blank (rare early rows)
        try:
            d = xldate_as_datetime(float(v0), wb.datemode).date()
        except (XLDateError, ValueError):
            continue
        rec = AAIISentimentRecord(
            date=d,
            bullish_pct=_to_pct(bull),
            neutral_pct=_to_pct(neu),
            bearish_pct=_to_pct(bear),
        )
        total = rec.bullish_pct + rec.neutral_pct + rec.bearish_pct
        if abs(total - Decimal("100")) > _SUM_TOL:
            # A real weekly row always sums to ~100. A row that does
            # not is a parse/format defect, not data — skip it rather
            # than persist a corrupt observation.
            logger.warning(
                "aaii.parse.row_sum_off",
                row=r, date=d.isoformat(), total=str(total),
            )
            continue
        records.append(rec)

    if not records:
        raise DataProviderOutage(
            f"{_PROVIDER_NAME} malformed workbook: no parseable weekly rows"
        )
    records.sort(key=lambda x: x.date)
    return records


class AAIIAdapter:
    """AAII Sentiment Survey adapter (no-auth, single-workbook)."""

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

    async def __aenter__(self) -> AAIIAdapter:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout,
                headers=_BROWSER_HEADERS, follow_redirects=True,
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
    async def get_sentiment_history(self) -> list[AAIISentimentRecord]:
        """Full weekly AAII sentiment history (1987→present), ascending
        by date. Raises ``DataProviderOutage`` on a network/permanent
        failure or a malformed/empty workbook."""
        try:
            content = await self._fetch_raw(_SURVEY_PATH)
        except DataProviderOutage:
            raise
        except httpx.HTTPError as exc:
            raise DataProviderOutage(
                f"{_PROVIDER_NAME} get_sentiment_history unreachable: {exc}"
            ) from exc
        return parse_sentiment_workbook(content)

    async def latest_published(self) -> date | None:
        """Cheap publication-availability probe (#165 facet 4): HEAD the
        survey file and read ``Last-Modified`` — the date AAII last
        published — WITHOUT downloading the 1 MB workbook. Lets a
        freshness check distinguish "we are stale (our defect → heal)"
        from "AAII simply hasn't published newer yet (vendor-late →
        quiet, no churn)". Returns ``None`` if the header is absent or
        the probe fails — caller falls back to the strict (assume-
        behind) behaviour, never silently green.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout,
                headers=_BROWSER_HEADERS, follow_redirects=True,
            )
            self._owned_client = True
        try:
            resp = await self._client.head(_SURVEY_PATH)
            lm = resp.headers.get("Last-Modified")
            if not lm:
                return None
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(lm).date()
        except (httpx.HTTPError, ValueError, TypeError):
            return None

    # ── Internal ───────────────────────────────────────────────────────
    @with_retry(max_attempts=3, backoff_base_sec=2.0, backoff_cap_sec=30.0)
    async def _fetch_raw(self, path: str) -> bytes:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout,
                headers=_BROWSER_HEADERS, follow_redirects=True,
            )
            self._owned_client = True
        resp = await self._client.get(path)
        if resp.status_code == 200:
            return resp.content
        # 429/5xx → retry (transient); 403 anti-bot is permanent per
        # the canonical with_retry contract — surfaces as outage.
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            raise httpx.HTTPStatusError(
                f"{_PROVIDER_NAME} {path} → {resp.status_code}",
                request=resp.request, response=resp,
            )
        raise DataProviderOutage(
            f"{_PROVIDER_NAME} {path} returned {resp.status_code}: "
            f"{resp.text[:160]}"
        )


__all__ = ["AAIIAdapter", "AAIISentimentRecord", "parse_sentiment_workbook"]
