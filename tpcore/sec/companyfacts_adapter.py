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


    @staticmethod
    def extract_filing_metadata(submissions: dict) -> dict:
        """Extract issuer reporting-model evidence from a SEC EDGAR
        ``submissions.json`` payload (P0-002, 2026-05-30 expert plan).

        IMPORTANT — wrong endpoint correction during the P0 build: the
        original P0 spec said "extract from DEI block in companyfacts"
        but the live SEC companyfacts payload only exposes
        ``dei.EntityCommonStockSharesOutstanding`` and
        ``dei.EntityPublicFloat``. The dispositive issuer-class signals
        (DocumentType histogram, fiscal year end, first/last filing
        dates) live on the SEC ``submissions/CIK<cik>.json`` endpoint
        which the existing ``edgar_adapter.py`` already fetches.

        SEC submissions.json shape (verified for CIK 0000320193 / AAPL
        2026-05-30):

            {
              "name": "Apple Inc.",
              "fiscalYearEnd": "0926",          # MMDD, AAPL fy ends Sep
              "category": "Large accelerated filer",
              "filings": {
                "recent": {
                  "form": ["10-Q","8-K","4","20-F",...],
                  "filingDate": ["2026-05-12",...],
                  "reportDate": ["2026-03-29",...]
                }
              }
            }

        Returns a structured dict — every field is ``None`` when the
        submissions payload doesn't carry the signal (no guessing). The
        caller persists these to ``platform.ticker_classifications``
        columns ``sec_document_type_primary``, ``sec_document_type_
        history``, ``first_public_filing_date``, ``fiscal_year_end_
        month``, ``last_filing_date``.

        Derivation rules:

          * ``document_type_primary``: the most-frequent
            **periodic-report** form (10-Q / 10-K / 20-F / 40-F / 10-Q/A
            / 10-K/A). Excludes 8-K, 4, 144, S-* registration filings,
            etc. — they are not the issuer's primary reporting cadence
            and would corrupt the histogram. Tie-break by most-recent.

          * ``document_type_history``: full histogram across ALL forms
            (not filtered) for diagnostics.

          * ``first_public_filing_date``: min(reportDate) over rows
            where form == primary. Anchors on the primary periodic
            report (not on first 8-K) so SPACs and post-IPO entities
            are dated correctly.

          * ``last_filing_date``: max(filingDate) across ALL rows.
            Used downstream (P2) to corroborate delisting via
            cessation of filings.

          * ``fiscal_year_end_month``: parse the month component out
            of the top-level ``fiscalYearEnd`` field, format ``"MMDD"``
            (e.g. ``"0926"`` → 9, ``"1231"`` → 12, ``"0831"`` → 8).

        Returns:
            ``{
                'document_type_primary': str | None,
                'document_type_history': dict[str, int] | None,
                'first_public_filing_date': date | None,
                'last_filing_date': date | None,
                'fiscal_year_end_month': int | None,
            }``

        Limitations:
          * ``filings.recent`` carries only the most-recent ~1000
            filings. Companies with > 1000 filings have older entries
            in ``filings.files[]`` (paginated). For our currently-
            failing 25 tickers all post-2010 IPOs, recent covers their
            entire history. For first_public_filing_date of long-lived
            companies (AAPL 1995-onward), recent only captures the
            last ~8 years — that's a known P0 limitation; full-history
            pagination is a P1 follow-up.
        """
        from collections import Counter
        from datetime import date as _date

        # Periodic-report forms we treat as primary candidates.
        _PERIODIC_FORMS = {
            "10-Q", "10-K", "10-Q/A", "10-K/A",
            "20-F", "20-F/A", "40-F", "40-F/A",
        }

        filings = submissions.get("filings") or {}
        recent = filings.get("recent") or {}
        forms = list(recent.get("form") or [])
        filing_dates = list(recent.get("filingDate") or [])
        report_dates = list(recent.get("reportDate") or [])

        if not forms:
            return {
                "document_type_primary": None,
                "document_type_history": None,
                "first_public_filing_date": None,
                "last_filing_date": None,
                "fiscal_year_end_month": _parse_fiscal_year_end_mmdd(
                    submissions.get("fiscalYearEnd"),
                ),
            }

        # Align parallel arrays defensively — SEC keeps them in lock-
        # step but slice to the shortest length just in case.
        n = min(len(forms), len(filing_dates), len(report_dates))
        forms = forms[:n]
        filing_dates = filing_dates[:n]
        report_dates = report_dates[:n]

        full_histogram: Counter = Counter(forms)

        # Periodic-report subset for primary classification.
        # ``strict=False``: arrays sliced to common ``n`` above so they
        # are length-aligned; the defensive slice is the invariant, not
        # the zip strictness.
        periodic_rows: list[tuple[str, _date | None, _date | None]] = []
        for f, fd, rd in zip(forms, filing_dates, report_dates, strict=False):
            if f not in _PERIODIC_FORMS:
                continue
            try:
                fd_d = _date.fromisoformat(fd) if fd else None
            except (ValueError, TypeError):
                fd_d = None
            try:
                rd_d = _date.fromisoformat(rd) if rd else None
            except (ValueError, TypeError):
                rd_d = None
            periodic_rows.append((f, fd_d, rd_d))

        primary: str | None = None
        first_filing: _date | None = None
        if periodic_rows:
            periodic_counter: Counter = Counter(
                r[0] for r in periodic_rows
            )
            # Collapse the /A amendment variants into the base form for
            # primary-type classification (10-Q/A → counts toward 10-Q).
            base_counter: Counter = Counter()
            for form, count in periodic_counter.items():
                base = form.rstrip("/A").rstrip("/")
                base_counter[base] += count

            max_count = max(base_counter.values())
            top = [v for v, c in base_counter.items() if c == max_count]
            if len(top) == 1:
                primary = top[0]
            else:
                # Tie-break — pick the base whose most-recent filing is
                # newest. Compare on filingDate.
                most_recent_by_base: dict[str, _date] = {}
                for form, fd, _rd in periodic_rows:
                    base = form.rstrip("/A").rstrip("/")
                    if base in top and fd is not None:
                        cur = most_recent_by_base.get(base)
                        if cur is None or fd > cur:
                            most_recent_by_base[base] = fd
                if most_recent_by_base:
                    primary = max(
                        most_recent_by_base,
                        key=lambda k: most_recent_by_base[k],
                    )

            if primary:
                primary_report_dates = [
                    rd for f, _fd, rd in periodic_rows
                    if f.rstrip("/A").rstrip("/") == primary and rd is not None
                ]
                if primary_report_dates:
                    first_filing = min(primary_report_dates)

        # last_filing_date — over ALL forms (not just periodic).
        last_filing: _date | None = None
        all_fd = []
        for fd in filing_dates:
            try:
                d = _date.fromisoformat(fd) if fd else None
                if d:
                    all_fd.append(d)
            except (ValueError, TypeError):
                continue
        if all_fd:
            last_filing = max(all_fd)

        return {
            "document_type_primary": primary,
            "document_type_history": dict(full_histogram),
            "first_public_filing_date": first_filing,
            "last_filing_date": last_filing,
            "fiscal_year_end_month": _parse_fiscal_year_end_mmdd(
                submissions.get("fiscalYearEnd"),
            ),
        }

    @staticmethod
    def extract_lifecycle_events(
        submissions: dict, *, cik: str | None = None,
    ) -> dict:
        """Public wrapper for ``_extract_lifecycle_events``
        (P2a 2026-05-30).

        Mirrors the ``extract_filing_metadata`` shape so the call
        sites use a consistent ``SECCompanyFactsAdapter.extract_*``
        pattern. See the module-level ``_extract_lifecycle_events``
        for the return-shape contract.
        """
        return _extract_lifecycle_events(submissions, cik=cik)

    async def get_submissions_cached(
        self,
        cik: str,
        *,
        cache_dir: str | None = None,
        force_refresh: bool = False,
    ) -> dict | None:
        """Cache-first variant of ``get_submissions`` (P2a 2026-05-30).

        Reads ``<cache_dir>/CIK<padded>.json`` if present (and not
        ``force_refresh``); falls back to SEC HTTP otherwise and
        persists the payload to disk for the next run.

        Cache layout intentionally mirrors SEC's URL shape so the
        on-disk files are self-describing and trivially comparable
        against a fresh pull.

        Args:
            cik: zero-pad-tolerant CIK string.
            cache_dir: cache root path. If None, resolves to
                ``os.environ['TP_DATA_DIR'] + '/sec_submissions'`` if
                ``TP_DATA_DIR`` is set, else ``./data/sec_submissions``
                relative to CWD. The directory is created on first
                write.
            force_refresh: bypass cache (re-fetch + overwrite).

        Returns the parsed dict, or ``None`` if SEC returned 404 — in
        which case a sentinel ``"__sec_404__": true`` is cached so
        future runs short-circuit without re-hitting SEC.
        """
        import json
        import os as _os
        from pathlib import Path

        cik_padded = str(cik).lstrip("0").zfill(10)
        if cache_dir is None:
            tp_data = _os.environ.get("TP_DATA_DIR")
            cache_dir = (
                f"{tp_data}/sec_submissions" if tp_data
                else "data/sec_submissions"
            )
        cache_path = Path(cache_dir) / f"CIK{cik_padded}.json"
        if cache_path.exists() and not force_refresh:
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "sec.submissions.cache_read_error",
                    cik=cik_padded, error=str(exc),
                )
                payload = None
            if isinstance(payload, dict):
                if payload.get("__sec_404__") is True:
                    return None
                return payload
            # Fallthrough on bad cache → re-fetch.

        payload = await self.get_submissions(cik)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if payload is None:
                cache_path.write_text(
                    json.dumps({"__sec_404__": True}),
                    encoding="utf-8",
                )
            else:
                cache_path.write_text(
                    json.dumps(payload), encoding="utf-8",
                )
        except OSError as exc:
            logger.warning(
                "sec.submissions.cache_write_error",
                cik=cik_padded, error=str(exc),
            )
        return payload

    async def get_submissions(self, cik: str) -> dict | None:
        """Fetch ``submissions/CIK<cik>.json`` from data.sec.gov
        (P0-002 2026-05-30).

        Companyfacts is at ``data.sec.gov/api/xbrl/companyfacts/`` —
        submissions live at ``data.sec.gov/submissions/`` (sibling
        path under the same host). The shared base_url on this
        adapter is ``https://data.sec.gov`` so a relative path
        works. Returns the parsed dict, or ``None`` if SEC returned
        404 (CIK exists in our DB but SEC doesn't have a submissions
        index — rare; usually means the CIK is wrong)."""
        if self._client is None:
            raise RuntimeError(
                "SECCompanyFactsAdapter must be used as a context manager"
            )
        cik_padded = str(cik).lstrip("0").zfill(10)
        url = f"/submissions/CIK{cik_padded}.json"
        try:
            resp = await self._client.get(url)
        except httpx.RequestError as exc:
            logger.warning(
                "sec.submissions.fetch_error",
                cik=cik_padded, error=str(exc),
            )
            return None
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            logger.warning(
                "sec.submissions.unexpected_status",
                cik=cik_padded, status=resp.status_code,
            )
            return None
        return resp.json()


# P2a (2026-05-30): SEC Form 25 / Form 15 evidence — periodic-lifecycle
# event signals. Form 25 ("Notification of Removal from Listing") is the
# delist notice; Form 15 ("Certification and Notice of Termination of
# Registration") is the SEC-reporting-obligation termination. State
# precedence: any Form 15 → 'deregistered'; Form 25 only → 'delist_effective'.
_FORM_25_VARIANTS: frozenset[str] = frozenset({"25", "25-NSE"})
_FORM_15_VARIANTS: frozenset[str] = frozenset({
    "15", "15-12G", "15-12B", "15F", "15-15D",
})
_LIFECYCLE_FORM_VARIANTS: frozenset[str] = (
    _FORM_25_VARIANTS | _FORM_15_VARIANTS
)


def _build_sec_filing_url(cik: str | None, accession: str | None) -> str | None:
    """Construct the canonical SEC Archives URL for a filing
    (P2a 2026-05-30).

    Format::

        https://www.sec.gov/Archives/edgar/data/<cik_int>/
            <accession_no_dashes>/<accession_with_dashes>-index.htm

    Both ``cik`` and ``accession`` must be present + well-formed.
    Returns ``None`` for any malformed input — operator hard rule:
    "NULL+evidence > guessing". The empty/wrong-format URL is worse
    than no URL.

    Accession numbers come from SEC as ``XXXXXXXXXX-XX-XXXXXX``
    (18 digits + 2 dashes). The Archives path strips the dashes for
    the directory name; the file basename keeps them.
    """
    if not cik or not accession:
        return None
    if not isinstance(cik, str) or not isinstance(accession, str):
        return None
    try:
        cik_int = int(cik.lstrip("0") or "0")
    except ValueError:
        return None
    if cik_int <= 0:
        return None
    # Accession format: 10 digits + "-" + 2 digits + "-" + 6 digits
    parts = accession.split("-")
    if len(parts) != 3:
        return None
    if not (len(parts[0]) == 10 and len(parts[1]) == 2
            and len(parts[2]) == 6):
        return None
    if not all(p.isdigit() for p in parts):
        return None
    no_dashes = "".join(parts)
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
        f"{no_dashes}/{accession}-index.htm"
    )


def _extract_lifecycle_events(
    submissions: dict, *, cik: str | None = None,
) -> dict:
    """Pull Form 25 + Form 15 events from a SEC submissions.json payload
    (P2a 2026-05-30).

    Walks ``submissions["filings"]["recent"]`` arrays in lock-step
    (form / filingDate / reportDate / accessionNumber) and collects
    every Form 25 / Form 15 event encountered. Returns the **list of
    all events** plus a **derived projection** for the operator-facing
    ``ticker_classifications`` columns.

    Args:
        submissions: parsed submissions.json dict.
        cik: CIK passed through to ``_build_sec_filing_url``. If None,
             the per-event ``evidence_url`` field is None even when an
             accessionNumber is present.

    Returns dict shape::

        {
          "form_25_events": [
              {"form": "25", "filing_date": date, "report_date": date,
               "accession_number": "...", "evidence_url": "https://..."},
              ...
          ],
          "form_15_events": [...same shape...],
          "derived_state": "deregistered" | "delist_effective" | None,
          "derived_event_date": date | None,  # report_date if present
                                                else filing_date of the
                                                latest LATEST event
                                                contributing to the state
          "derived_source": "sec_form_15" | "sec_form_25" | None,
          "derived_evidence_url": str | None,  # latest event's URL
        }

    State derivation precedence:
        * Any Form 15 present → 'deregistered' (terminal state — SEC
          reporting obligation ended)
        * Form 25 present + no Form 15 → 'delist_effective'
        * Neither → None (don't overwrite — operator hard rule:
          NULL > guessing)
    """
    from datetime import date as _date  # noqa: PLR0402

    filings = submissions.get("filings") or {}
    recent = filings.get("recent") or {}
    forms = list(recent.get("form") or [])
    filing_dates = list(recent.get("filingDate") or [])
    report_dates = list(recent.get("reportDate") or [])
    accessions = list(recent.get("accessionNumber") or [])

    n = min(len(forms), len(filing_dates),
            len(report_dates), len(accessions))
    forms = forms[:n]
    filing_dates = filing_dates[:n]
    report_dates = report_dates[:n]
    accessions = accessions[:n]

    form_25_events: list[dict] = []
    form_15_events: list[dict] = []

    for form, fd, rd, acc in zip(
        forms, filing_dates, report_dates, accessions, strict=False,
    ):
        if form not in _LIFECYCLE_FORM_VARIANTS:
            continue
        try:
            fd_d = _date.fromisoformat(fd) if fd else None
        except (ValueError, TypeError):
            fd_d = None
        try:
            rd_d = _date.fromisoformat(rd) if rd else None
        except (ValueError, TypeError):
            rd_d = None
        event = {
            "form": form,
            "filing_date": fd_d,
            "report_date": rd_d,
            "accession_number": acc if isinstance(acc, str) else None,
            "evidence_url": _build_sec_filing_url(cik, acc),
        }
        if form in _FORM_25_VARIANTS:
            form_25_events.append(event)
        else:  # Form 15 variant
            form_15_events.append(event)

    # State derivation — Form 15 is terminal, always wins.
    derived_state: str | None = None
    derived_source: str | None = None
    contributing: list[dict] = []
    if form_15_events:
        derived_state = "deregistered"
        derived_source = "sec_form_15"
        contributing = form_15_events
    elif form_25_events:
        derived_state = "delist_effective"
        derived_source = "sec_form_25"
        contributing = form_25_events

    derived_event_date: _date | None = None
    derived_evidence_url: str | None = None
    if contributing:
        # Latest event: prefer the most-recent filing_date as the
        # ordering key (report_date may be in the future and inconsistent
        # across issuers). For the canonical event_date itself, prefer
        # report_date if present (issuer's claimed effective date) else
        # filing_date.
        def _sort_key(e: dict) -> _date:
            fd_v = e["filing_date"]
            return fd_v if fd_v is not None else _date.min
        contributing_sorted = sorted(contributing, key=_sort_key, reverse=True)
        latest = contributing_sorted[0]
        derived_event_date = (
            latest["report_date"] or latest["filing_date"]
        )
        derived_evidence_url = latest["evidence_url"]

    return {
        "form_25_events": form_25_events,
        "form_15_events": form_15_events,
        "derived_state": derived_state,
        "derived_source": derived_source,
        "derived_event_date": derived_event_date,
        "derived_evidence_url": derived_evidence_url,
    }


def _parse_fiscal_year_end_mmdd(raw: str | None) -> int | None:
    """Parse SEC submissions.json ``fiscalYearEnd`` field. Format is
    ``MMDD`` (4-char zero-padded, e.g. ``"0926"`` for September 26).
    Returns the month component as 1-12, or ``None`` if the format
    is unexpected. Never guesses."""
    if not raw or not isinstance(raw, str) or len(raw) != 4:
        return None
    try:
        month = int(raw[:2])
    except ValueError:
        return None
    return month if 1 <= month <= 12 else None


__all__ = [
    "SECCompanyFactsAdapter",
    "SEC_DATA_BASE_URL",
    "SEC_DATA_BASE_URL_ENV",
    "SEC_USER_AGENT_ENV",
    "REVENUE_KEYS",
    "SHARES_KEYS",
]
