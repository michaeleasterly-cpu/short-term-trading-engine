"""SEC EDGAR 8-K corporate-events extractor (corp-history P3, MVP).

Per spec docs/superpowers/specs/2026-05-24-corporate-history-enrichment.md
§5 P3 + expert validation (~70% recall target on the 15-row truth-set
fixture). This module is the deterministic extractor; live polling /
ops.py wiring is deferred until recall validation gates the design.

What it does:
  1. For a given CIK, find recent 8-K filings whose `items` include
     1.01 (Material Definitive Agreement) or 2.01 (Completion of
     Acquisition/Disposition).
  2. Fetch each filing's primary HTML body.
  3. Apply a regex-based parser to extract:
       - event_kind: 'merger' | 'acquisition' | 'take_private' |
         'spinoff' | 'bankruptcy_liquidation' | 'bankruptcy_reorg' | None
       - event_date: heuristic — use filing_date unless body says
         "effective date" explicitly.
       - acquirer / successor (free-text name — caller resolves to
         issuer_id / classification_id downstream).
       - cash_per_share + ratio_num/ratio_den when the body mentions
         "$N.NN per share" or "X shares of Y per Z shares of W".

What it does NOT do (expert verdict: not feasible from headers alone):
  - Exhibit-level (Ex-2.1 merger agreement) deep parsing — the body of
    the 8-K item-narrative is enough for the basic fields; complex deal
    terms (CVRs, election windows, multi-tranche) are out of scope.
  - 100% recall — expect ~70% on the truth-set per the expert review.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import date

    from tpcore.sec.edgar_adapter import SECEdgarAdapter

logger = structlog.get_logger(__name__)


# Items in 8-K that signal an M&A event.
_MA_ITEMS: frozenset[str] = frozenset({"1.01", "2.01", "1.02"})

# Optional items that flag terminal / bankruptcy events.
_BANKRUPTCY_ITEMS: frozenset[str] = frozenset({"1.03"})  # Item 1.03 = "Bankruptcy or Receivership"

# Event-kind detection patterns. Tried in order — first match wins.
# Each pattern is (event_kind, regex). regex is case-insensitive.
# Take-private MUST come before merger because take-private deals are
# legally structured as mergers (PE firm's merger-sub merges with target);
# the distinguishing signal is "private", "investment vehicle", "fund",
# "subsidiary of [PE firm]" — keyword-scoped before "merger agreement" hits.
_KIND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bankruptcy_liquidation",
     re.compile(r"chapter\s+7|liquidat(ing|ion)|wind[\s-]?down\s+plan", re.I)),
    ("bankruptcy_reorg",
     re.compile(r"chapter\s+11|bankruptcy\s+reorganization|debtor[\s-]in[\s-]possession", re.I)),
    ("spinoff",
     re.compile(r"spin[\s-]?off|spin[\s-]?out|distribution\s+of\s+(?:shares|common\s+stock)\s+of", re.I)),
    # Take-private: scoped to PE-style language. "private investment firm",
    # "private equity", "going private", "investment vehicle", "affiliate of"
    # + "investment fund". These distinguish PE take-private mergers from
    # strategic public-to-public mergers.
    ("take_private",
     re.compile(
         r"taken\s+private|going[\s-]?private|"
         r"private\s+investment\s+firm|private\s+equity|"
         r"affiliate(?:s)?\s+of\s+[A-Z][^.]{0,80}?(?:Partners|Capital|Holdings|Fund|Investment|Equity)|"
         r"investment\s+vehicle\s+(?:managed|owned|controlled)\s+by|"
         r"delisted\s+from.*following\s+the\s+(?:merger|acquisition)",
         re.I,
     )),
    ("merger",
     re.compile(r"merger\s+agreement|definitive\s+agreement.*?merge|merged\s+with|merger\s+is\s+complete", re.I)),
    ("acquisition",
     re.compile(r"(?:acquired|acquire|acquisition\s+(?:of|by))(?!\s+of\s+(?:assets|substantially))", re.I)),
    ("asset_sale",
     re.compile(r"(?:acquisition\s+of\s+(?:substantially\s+all\s+of\s+the\s+)?assets|asset\s+purchase\s+agreement)", re.I)),
)


# Acquirer extraction patterns. Tries to find "X acquired/acquires Y" or
# "merged into X" — first non-self-referential, non-boilerplate match wins.
# The (?<!\b(?:check\s+mark\s+)) negative lookbehind nukes the 8-K
# boilerplate ("check mark whether the registrant is an...") that was
# matching every filer in the v1 patterns. Word "the" and other articles
# are also filtered.
_BOILERPLATE_PREFIXES: tuple[str, ...] = (
    "check mark", "the registrant", "this report", "the issuer",
    "an emerging", "a large", "the company",
)


def _is_boilerplate(name: str) -> bool:
    """Reject 8-K boilerplate captures that masquerade as company names."""
    n = name.lower().strip()
    return any(n.startswith(p) for p in _BOILERPLATE_PREFIXES)


_ACQUIRER_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "Microsoft Corporation (NASDAQ: MSFT) ... has acquired" — strongest signal
    re.compile(r"([A-Z][\w\s&,\.]{3,80}?(?:Inc|Corp|Corporation|Company|LLC|Ltd|Holdings|Group|Partners|Capital|Bank)\.?)\s+(?:has\s+)?(?:completed\s+(?:its\s+)?acquisition|acquired|will\s+acquire|to\s+acquire)\b", re.I),
    # "merged with X" / "merged into X"
    re.compile(r"\bmerged\s+(?:with|into)\s+(?:and\s+into\s+)?([A-Z][\w\s&,\.]{3,80}?(?:Inc|Corp|Corporation|Company|LLC|Ltd|Holdings|Group|Partners|Capital|Bank)\.?)", re.I),
    # "wholly-owned subsidiary of X" — PE take-private + strategic acquisition pattern
    re.compile(r"wholly[\s-]?owned\s+subsidiary\s+of\s+([A-Z][\w\s&,\.]{3,80}?(?:Inc|Corp|Corporation|Company|LLC|Ltd|Holdings|Group|Partners|Capital|Bank)\.?)", re.I),
    # "by X" — lowest-priority because most likely to hit boilerplate
    re.compile(r"\bby\s+([A-Z][\w\s&,\.]{3,80}?(?:Inc|Corp|Corporation|Company|LLC|Ltd|Holdings|Group|Partners|Capital|Bank)\.?)", re.I),
)


# Cash-per-share extraction.
_CASH_PER_SHARE_PATTERN: re.Pattern[str] = re.compile(
    r"\$\s*(\d{1,4}(?:[,]\d{3})*(?:\.\d{1,4})?)\s+(?:per\s+share|in\s+cash\s+per\s+share|cash\s+per\s+share|per\s+(?:share\s+of\s+)?common\s+stock)",
    re.I,
)


# Effective-date extraction. Multiple patterns; takes the latest date
# found (most likely the actual close).
_EFFECTIVE_DATE_PATTERN: re.Pattern[str] = re.compile(
    r"(?:effective\s+date\s+of|effective\s+(?:on|as\s+of)|closing\s+date\s+of|closed\s+on)\s+"
    r"(?:the\s+)?([A-Z][a-z]+\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})",
    re.I,
)


@dataclass(frozen=True)
class ParsedEvent:
    """One M&A event extracted from a single 8-K filing.

    Caller resolves `acquirer_name` to an issuer_id / classification_id
    downstream (this module deliberately doesn't reach into the issuer
    graph — separation of concerns).
    """

    cik: str                          # the FILER's CIK (the security that's the subject)
    filing_date: date                 # 8-K filing date
    accession_number: str
    event_kind: str | None            # None if no kind matched (low confidence)
    event_date: date                  # filing_date unless body says otherwise
    acquirer_name: str | None
    cash_per_share: float | None
    items: list[str]                  # the 8-K item-codes (1.01, 2.01, etc.)
    excerpt: str                      # first 500 chars of the parsed text — debug aid


def _strip_html(html: str) -> str:
    """Minimal HTML→text: drop tags + collapse whitespace.

    BeautifulSoup is heavyweight for our purposes; the 8-K bodies are
    well-formed enough that a regex pass + entity-decode hits ~95% of
    the useful text. We accept ~5% noise (table cells smushed together)
    in exchange for zero new deps.
    """
    import html as html_mod
    # Drop <script> + <style> blocks first
    s = re.sub(r"<(?:script|style)[^>]*>.*?</(?:script|style)>", " ", html, flags=re.I | re.S)
    # Convert <br> and block-element closes to spaces
    s = re.sub(r"<(?:br|/p|/div|/td|/tr|/li)[^>]*>", " ", s, flags=re.I)
    # Strip remaining tags
    s = re.sub(r"<[^>]+>", " ", s)
    # Decode HTML entities
    s = html_mod.unescape(s)
    # Collapse whitespace
    return re.sub(r"\s+", " ", s).strip()


def _detect_event_kind(text: str, items: list[str]) -> str | None:
    """Apply pattern set in priority order; first match wins.

    Item 1.03 short-circuits to bankruptcy (no need for keyword search).
    """
    if any(it in _BANKRUPTCY_ITEMS for it in items):
        # Item 1.03 + chapter-11 keyword → reorg; otherwise default to liquidation
        if re.search(r"chapter\s+11", text, re.I):
            return "bankruptcy_reorg"
        return "bankruptcy_liquidation"

    for kind, pattern in _KIND_PATTERNS:
        if pattern.search(text):
            return kind
    return None


def _extract_acquirer(text: str, filer_name_hint: str | None = None) -> str | None:
    """Find the most likely acquirer name from the 8-K text.

    `filer_name_hint`: when known, we exclude matches that == the filer
    (self-referential matches like "AcquiredCo entered into a merger
    agreement with X" — X is what we want, not AcquiredCo).
    """
    # SEC 8-K "Items Section" boilerplate runs to ~3-4 KB at the top — skip
    # past the cover page to the actual narrative body where M&A language lives.
    body_start = max(
        text.lower().find("item 1.01"),
        text.lower().find("item 2.01"),
        text.lower().find("item 1.03"),
        0,
    )
    search_text = text[body_start:] if body_start > 0 else text

    hint_lower = filer_name_hint.lower() if filer_name_hint else ""
    for pat in _ACQUIRER_PATTERNS:
        for m in pat.finditer(search_text):
            name = m.group(1).strip()
            name_lower = name.lower()
            # Skip self-referential / filer matches — contains, not just startswith
            # (TWTR's 8-K matches "and among Twitter, Inc." as the captured name).
            if hint_lower and hint_lower in name_lower:
                continue
            # Skip "Company" / "Issuer" / "Registrant" — generic self-references
            # that aren't filtered by the boilerplate prefix check.
            if any(generic in name_lower for generic in (
                "the company", "the issuer", "the registrant",
                "the parent", "continuing to exist", "and among",
            )):
                continue
            if len(name) < 6 or len(name) > 80:
                continue
            if _is_boilerplate(name):
                continue
            return name
    return None


# spaCy NER fallback for acquirer extraction. Lazy import + lazy load —
# spaCy + en_core_web_sm cost ~150 MB but are only loaded when the regex
# extractor returns None on a filing. Cached at module level after first load
# so subsequent filings in the same process re-use the loaded model.
_NER_MODEL = None


def _get_ner_model() -> object | None:
    """Lazy-load spaCy en_core_web_sm. Returns None if spaCy or the model
    isn't installed (caller falls back to regex-only behaviour)."""
    global _NER_MODEL  # noqa: PLW0603 — module-level cache
    if _NER_MODEL is not None:
        return _NER_MODEL
    try:
        import spacy  # type: ignore[import-untyped]  # lazy import
        _NER_MODEL = spacy.load("en_core_web_sm")
    except (ImportError, OSError) as e:
        logger.warning("sec.corp_events.spacy_unavailable", error=str(e))
        return None
    return _NER_MODEL


def _extract_acquirer_ner(
    text: str, filer_name_hint: str | None = None,
) -> str | None:
    """spaCy NER fallback for acquirer extraction.

    Strategy: run NER on the post-Item-1.01 narrative; collect ORG entities;
    skip the filer + boilerplate; prefer ORGs that appear NEAR M&A keywords.
    If no near-keyword ORG, fall back to the second distinct ORG (the first
    is usually the filer/target).

    Returns None when spaCy isn't available — caller transparently falls
    back to whatever regex returned.
    """
    nlp = _get_ner_model()
    if nlp is None:
        return None
    body_start = max(
        text.lower().find("item 1.01"),
        text.lower().find("item 2.01"),
        text.lower().find("item 1.03"),
        0,
    )
    # Cap input to ~30 KB — 8-K bodies can be huge with exhibits inlined.
    # spaCy is fast but loading + processing very long docs eats wall-clock.
    search_text = text[body_start:body_start + 30_000] if body_start > 0 else text[:30_000]

    doc = nlp(search_text)
    hint_lower = filer_name_hint.lower() if filer_name_hint else ""

    # M&A-keyword positions — used to score ORG candidates by proximity.
    keyword_pattern = re.compile(
        r"\b(?:merger|acquired|acquire|acquisition|merged|wholly[\s-]?owned\s+subsidiary)\b",
        re.I,
    )
    keyword_positions: list[int] = [m.start() for m in keyword_pattern.finditer(search_text)]

    # Merger-vehicle patterns to DEMOTE: NER tags these as ORG but they're
    # the legal vehicle, not the actual acquirer. The acquirer is the PARENT
    # of the merger sub — we want that, not the sub.
    merger_vehicle_substrings: tuple[str, ...] = (
        "merger sub", "merger agreement", "acquisition sub",
        "bankruptcy court", "delaware court", "supreme court",
        "the board", "board of directors", "compensation committee",
        "event of default", "court", "the offer",
    )

    candidates: list[tuple[int, int, str]] = []  # (priority_tier, proximity, name)
    seen: set[str] = set()
    for ent in doc.ents:
        if ent.label_ != "ORG":
            continue
        name = ent.text.strip()
        name_lower = name.lower()
        # Length sanity
        if len(name) < 6 or len(name) > 80:
            continue
        # Skip self-referential / filer matches
        if hint_lower and hint_lower in name_lower:
            continue
        if _is_boilerplate(name):
            continue
        # Skip generic placeholders that NER sometimes labels ORG
        if name_lower in ("the company", "the parent", "the issuer",
                          "the registrant", "common stock"):
            continue
        # Dedup
        if name_lower in seen:
            continue
        seen.add(name_lower)
        # Tier: 0 = real-company ORG; 1 = merger-vehicle / court / committee.
        # Always prefer tier 0 over tier 1 regardless of proximity, because
        # tier-1 matches are structurally not acquirers.
        is_vehicle = any(sub in name_lower for sub in merger_vehicle_substrings)
        tier = 1 if is_vehicle else 0
        # Proximity score: smallest distance to any M&A keyword (lower = better).
        ent_start = ent.start_char
        if keyword_positions:
            proximity = min(abs(ent_start - kp) for kp in keyword_positions)
        else:
            proximity = ent_start  # no keywords — use document position
        candidates.append((tier, proximity, name))

    if not candidates:
        return None

    # Prefer tier 0 (real companies) over tier 1 (merger vehicles); within
    # the chosen tier, prefer closest-to-M&A-keyword.
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2]


# PE / take-private firm name hallmarks. When the acquirer's name ends
# in one of these AND the deal is cash-only (no exchange ratio), the
# kind heuristic upgrades from 'merger' to 'take_private'.
_PE_SUFFIX_PATTERN: re.Pattern[str] = re.compile(
    r"\b(?:Partners|Capital|Holdings\s+[IVX]+|Fund(?:s)?|Equity|Investment(?:s)?|"
    r"Management|Advisors|Investors|Ventures)\b\.?\s*$",
    re.I,
)


def _refine_kind_for_take_private(
    kind: str | None,
    acquirer_name: str | None,
    cash_per_share: float | None,
) -> str | None:
    """Post-hoc kind refinement: detect take-private deals that get
    classified as 'merger' by the keyword patterns (legal structure of
    a PE take-private IS a merger via a merger sub, so 'merger' matches
    first). Heuristic: cash-only deal AND acquirer name has PE-firm
    hallmarks => upgrade to 'take_private'."""
    if kind != "merger":
        return kind
    if cash_per_share is None:
        return kind
    if not acquirer_name:
        return kind
    if _PE_SUFFIX_PATTERN.search(acquirer_name):
        return "take_private"
    return kind


def _extract_cash_per_share(text: str) -> float | None:
    """Find $N.NN per share. Multiple matches → take the largest
    (the actual deal price; smaller amounts often refer to dividends
    or per-share earnings)."""
    matches = []
    for m in _CASH_PER_SHARE_PATTERN.finditer(text):
        try:
            val = float(m.group(1).replace(",", ""))
            if 0.01 <= val <= 10000:  # reasonable range for per-share prices
                matches.append(val)
        except ValueError:
            continue
    return max(matches) if matches else None


def _extract_event_date(text: str, fallback: date) -> date:
    """Find an explicit effective/closing date; fall back to filing_date."""
    import calendar
    from datetime import UTC, datetime
    from datetime import date as _date
    matches: list[date] = []
    for m in _EFFECTIVE_DATE_PATTERN.finditer(text):
        raw = m.group(1)
        if "-" in raw:
            try:
                matches.append(
                    datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC).date()
                )
            except ValueError:
                continue
        else:
            # "October 27, 2022" -> 2022-10-27
            mo_match = re.match(r"([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})", raw)
            if mo_match:
                month_name, day, year = mo_match.groups()
                try:
                    month_num = list(calendar.month_name).index(month_name)
                    matches.append(_date(int(year), month_num, int(day)))
                except (ValueError, IndexError):
                    continue
    return max(matches) if matches else fallback


async def _fetch_8k_body(
    adapter: SECEdgarAdapter,
    cik: int,
    accession_number: str,
    primary_document: str,
) -> str:
    """Fetch the primary 8-K body (HTML) and strip to text. Mirrors
    fetch_form4_xml's URL pattern."""
    import httpx
    clean_acc = accession_number.replace("-", "")
    url = (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{clean_acc}/{primary_document}"
    )
    client = await adapter._ensure_client()  # noqa: SLF001 — reusing adapter's client
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        return _strip_html(resp.text)
    except httpx.HTTPError as e:
        logger.warning("sec.corp_events.fetch_failed", cik=cik,
                       accession=accession_number, error=str(e))
        return ""


async def find_8k_ma_filings(
    adapter: SECEdgarAdapter,
    ticker: str,
    *,
    since: date | None = None,
    items_filter: Iterable[str] | None = None,
) -> list[dict[str, str | date | int | list[str]]]:
    """Find 8-K filings for `ticker` that contain M&A items (1.01, 2.01,
    1.02, or 1.03 bankruptcy). Returns raw submission-index rows with
    `items` populated.

    NOTE: this path uses ticker → CIK via SEC's company_tickers.json,
    which ONLY lists CURRENTLY-TRADING tickers. For delisted issuers
    (acquired, taken private, bankrupt) use find_8k_ma_filings_by_cik
    with the CIK directly."""
    raw = await adapter.get_recent_filings(
        ticker, forms=("8-K",), since=since, full_history=True,
    )
    target_items = frozenset(items_filter) if items_filter else (_MA_ITEMS | _BANKRUPTCY_ITEMS)
    return [
        r for r in raw
        if any(it.strip() in target_items for it in (r.get("items") or []))
    ]


def _parse_filing_date_str(raw: str) -> date | None:
    """Parse SEC's filing-date string formats. Mirrors the adapter's helper."""
    from datetime import UTC, datetime
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=UTC).date()
    except (ValueError, TypeError):
        return None


async def find_8k_ma_filings_by_cik(
    adapter: SECEdgarAdapter,
    cik: int,
    *,
    since: date | None = None,
    items_filter: Iterable[str] | None = None,
) -> list[dict[str, str | date | int | list[str]]]:
    """Find 8-K M&A filings by CIK directly — bypasses the ticker → CIK
    lookup that SEC's company_tickers.json gates (delisted issuers
    aren't in that map). This is the path for corp-history backfill
    against historical CIKs of delisted entities (TWTR, SPLK, etc.).
    """
    cik_padded = str(int(cik)).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    payload = await adapter._fetch_raw(url)  # noqa: SLF001 — reusing adapter HTTP
    filings = (payload or {}).get("filings", {}) or {}
    target_items = frozenset(items_filter) if items_filter else (_MA_ITEMS | _BANKRUPTCY_ITEMS)
    results: list[dict[str, str | date | int | list[str]]] = []

    def _emit(block: dict[str, list]) -> None:
        forms = block.get("form", []) or []
        dates = block.get("filingDate", []) or []
        accs = block.get("accessionNumber", []) or []
        prims = block.get("primaryDocument", []) or []
        items = block.get("items", []) or []
        for i, form in enumerate(forms):
            if str(form).upper() != "8-K":
                continue
            fd = _parse_filing_date_str(dates[i]) if i < len(dates) else None
            if fd is None or (since is not None and fd < since):
                continue
            row_items_raw = items[i] if i < len(items) else ""
            row_items = [s.strip() for s in str(row_items_raw).split(",") if s.strip()]
            if not any(it in target_items for it in row_items):
                continue
            results.append({
                "cik": cik,
                "form": "8-K",
                "filing_date": fd,
                "accession_number": str(accs[i]) if i < len(accs) else "",
                "primary_document": str(prims[i]) if i < len(prims) else "",
                "items": row_items,
            })

    _emit(filings.get("recent", {}) or {})
    # Older shards via filings.files (if full history requested + present)
    for shard in filings.get("files", []) or []:
        shard_url = f"https://data.sec.gov/submissions/{shard.get('name')}"
        try:
            shard_payload = await adapter._fetch_raw(shard_url)  # noqa: SLF001
        except Exception:  # noqa: BLE001
            continue
        _emit(shard_payload or {})
    return results


async def extract_for_cik(
    adapter: SECEdgarAdapter,
    cik: int,
    *,
    since: date | None = None,
    max_filings: int = 10,
    filer_name_hint: str | None = None,
) -> list[ParsedEvent]:
    """End-to-end extraction for one CIK — the delisted-issuer-safe path."""
    filings = await find_8k_ma_filings_by_cik(adapter, cik, since=since)
    filings = filings[:max_filings]
    out: list[ParsedEvent] = []
    for f in filings:
        body = await _fetch_8k_body(
            adapter, int(f["cik"]),
            str(f["accession_number"]), str(f["primary_document"]),
        )
        if not body:
            continue
        items = list(f.get("items") or [])
        kind = _detect_event_kind(body, items)
        # NER first (spaCy is more accurate on ORG entities); regex as fallback
        # for environments where spaCy / en_core_web_sm aren't installed.
        acquirer = _extract_acquirer_ner(body, filer_name_hint=filer_name_hint)
        if acquirer is None:
            acquirer = _extract_acquirer(body, filer_name_hint=filer_name_hint)
        cash = _extract_cash_per_share(body)
        evt_date = _extract_event_date(body, fallback=f["filing_date"])
        kind = _refine_kind_for_take_private(kind, acquirer, cash)
        out.append(ParsedEvent(
            cik=str(f["cik"]),
            filing_date=f["filing_date"],
            accession_number=str(f["accession_number"]),
            event_kind=kind,
            event_date=evt_date,
            acquirer_name=acquirer,
            cash_per_share=cash,
            items=items,
            excerpt=body[:500],
        ))
    return out


async def extract_for_ticker(
    adapter: SECEdgarAdapter,
    ticker: str,
    *,
    since: date | None = None,
    max_filings: int = 10,
) -> list[ParsedEvent]:
    """End-to-end extraction for one ticker: discover M&A 8-Ks, fetch each
    body, parse, return ParsedEvents."""
    filings = await find_8k_ma_filings(adapter, ticker, since=since)
    filings = filings[:max_filings]
    out: list[ParsedEvent] = []
    for f in filings:
        body = await _fetch_8k_body(
            adapter, int(f["cik"]),
            str(f["accession_number"]), str(f["primary_document"]),
        )
        if not body:
            continue
        items = list(f.get("items") or [])
        kind = _detect_event_kind(body, items)
        acquirer = _extract_acquirer(body, filer_name_hint=ticker)
        if acquirer is None:
            acquirer = _extract_acquirer_ner(body, filer_name_hint=ticker)
        cash = _extract_cash_per_share(body)
        evt_date = _extract_event_date(body, fallback=f["filing_date"])
        kind = _refine_kind_for_take_private(kind, acquirer, cash)
        out.append(ParsedEvent(
            cik=str(f["cik"]),
            filing_date=f["filing_date"],
            accession_number=str(f["accession_number"]),
            event_kind=kind,
            event_date=evt_date,
            acquirer_name=acquirer,
            cash_per_share=cash,
            items=items,
            excerpt=body[:500],
        ))
    return out


__all__ = [
    "ParsedEvent",
    "extract_for_cik",
    "extract_for_ticker",
    "find_8k_ma_filings",
    "find_8k_ma_filings_by_cik",
]
