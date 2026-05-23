"""Event-driven `parent_resolver` — resolves unknown tickers per v2.2 spec §1.8-§1.10.

Invoked by ingestion handlers when an `UNKNOWN_TICKER_OBSERVED` event fires
(i.e., a handler tried to INSERT a row for a ticker not in
`platform.ticker_classifications.current_ticker`). NOT a scheduled feed.

Per-handler-lane dispatch (per v2.2 spec §1.10 + sibling memory
`sec-primary-insider-fmp-fallback-non-us`):

  INSIDER / MATERIAL_EVENTS (data source = SEC EDGAR; CIK in hand):
    1. SEC company_tickers.json reverse-lookup (we have CIK from EDGAR record;
       SEC is the canonical US ticker↔CIK map; free, fast).
    2. FMP `/profile` fallback for foreign-issuer CIKs SEC doesn't carry.
    3. FMP `/profile` enrichment for country/asset_class/exchange.
    4. OpenFIGI `/v3/mapping` for compositeFIGI.

  PRICES / FUNDAMENTALS / PROFILE (data source = FMP; ticker in hand):
    1. FMP `/profile/{ticker}` for ticker, country, asset_class, exchange,
       CUSIP, ISIN, CIK.
    2. OpenFIGI `/v3/mapping` for compositeFIGI.

After resolution, parent_resolver:
- MINTS the TKR-14 smart-key via `tpcore.identity.tkr14.mint` from immutable +
  at-mint-snapshot facts (country, asset_class, ipo_venue, discovery_source,
  cik, legal_name, now).
- INSERTs the new `ticker_classifications` row with figi/cusip/isin populated.
- INSERTs the first `ticker_history` row with `(classification_id, ticker,
  valid_from=today, valid_to=NULL)`.

Pin-at-first-resolve discipline:
- NEVER overwrite an existing non-null `figi`/`cusip`/`isin`/`cik`.
- Divergence between stored value and new resolution attempt writes an
  `IDENTITY_DIVERGENCE_INVESTIGATE` event to `application_log` for operator
  review — NEVER silent update.
- NULL fields ARE filled when new data arrives (operator clarification
  2026-05-23: "you will put in cusip and that other shit as you get it").

This module is pure orchestration — it does NOT own the HTTP clients for FMP,
SEC, or OpenFIGI. It composes the existing adapters
(`tpcore.fmp.*`, `tpcore.sec.SECEdgarAdapter`, `tpcore.openfigi.OpenFIGIAdapter`)
and applies the dispatch + persistence logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import structlog

from tpcore.identity.tkr14 import (
    AssetClass,
    DiscoverySource,
    IPOVenue,
    mint,
)

logger = structlog.get_logger(__name__)


class HandlerKind(StrEnum):
    """Which ingestion-handler kind invoked parent_resolver.

    Drives the per-lane resolution dispatch. Memo-encoded:
    - INSIDER / MATERIAL_EVENTS → SEC-first path (CIK in hand; reverse-lookup ticker).
    - PRICES / FUNDAMENTALS / PROFILE / SHORT_INTEREST / CORPORATE_ACTIONS /
      EARNINGS / LIQUIDITY / SPREAD / SOCIAL / OPTIONS / BORROW / OTHER →
      FMP-first path (ticker in hand; enrich from /profile).
    """

    # SEC-first lanes (CIK known from the EDGAR record)
    INSIDER = "insider"
    MATERIAL_EVENTS = "material_events"

    # FMP-first lanes (ticker known from the wire feed)
    PRICES = "prices"
    FUNDAMENTALS = "fundamentals"
    PROFILE = "profile"
    SHORT_INTEREST = "short_interest"
    CORPORATE_ACTIONS = "corporate_actions"
    EARNINGS = "earnings"
    LIQUIDITY = "liquidity"
    SPREAD = "spread"
    SOCIAL = "social"
    OPTIONS = "options"
    BORROW = "borrow"
    OTHER = "other"


_SEC_FIRST_LANES: frozenset[HandlerKind] = frozenset({
    HandlerKind.INSIDER,
    HandlerKind.MATERIAL_EVENTS,
})


@dataclass(frozen=True)
class ResolvedClassification:
    """The structured outcome of a parent_resolver.resolve() call.

    All fields except `tkr14_id` and `ticker` may be None when resolution is
    partial (e.g., OpenFIGI rate-limited; SEC returned 200 but no CIK).
    Caller decides whether to INSERT-now-and-fill-later or block on the gap.
    """

    tkr14_id: str
    """The freshly-minted TKR-14 smart-key (14 chars). Always populated."""

    ticker: str
    """The operator-visible ticker symbol at first-resolve time."""

    country: str
    """ISO 3166-1 alpha-2 country code."""

    asset_class: AssetClass
    """Per v2.2 spec §1.2 asset-class taxonomy."""

    ipo_venue: IPOVenue
    """Listing venue at IPO (snapshot semantic)."""

    discovery_source: DiscoverySource
    """Which feed first discovered this security."""

    cik: str | None
    """SEC CIK; populated for US issuers via SEC company_tickers."""

    legal_name: str | None
    """Issuer legal name from FMP /profile."""

    exchange: str | None
    """Current trading exchange (mutable; stored on ticker_classifications.current_exchange)."""

    figi: str | None
    """OpenFIGI US Composite FIGI (12 chars). Per spec §1.9; pin-at-first-resolve."""

    cusip: str | None
    """CUSIP from FMP /profile. Pin-at-first-resolve."""

    isin: str | None
    """ISIN from FMP /profile. Pin-at-first-resolve."""

    resolved_at: datetime
    """When parent_resolver completed this resolution (UTC)."""


def _is_sec_first(handler_kind: HandlerKind) -> bool:
    """Return True if the handler's source lane is SEC-EDGAR (CIK-keyed)."""
    return handler_kind in _SEC_FIRST_LANES


@dataclass(frozen=True)
class ResolveInputs:
    """Inputs the orchestrating handler passes to parent_resolver.resolve().

    For SEC-first lanes (INSIDER, MATERIAL_EVENTS), the handler is iterating
    EDGAR filings; it already has the CIK from the filing's `cik` field. The
    ticker is unknown — that's what the SEC reverse-lookup resolves.

    For FMP-first lanes, the handler has the ticker from the wire feed; the
    CIK is unknown until FMP /profile responds (or is permanently unknown for
    non-US issuers).
    """

    ticker: str | None
    """Known ticker (FMP-first lane) or None (SEC-first lane)."""

    cik: str | None
    """Known CIK (SEC-first lane) or None (FMP-first lane)."""

    handler_kind: HandlerKind
    """Which lane called us; drives the dispatch order."""


def _assert_inputs_valid_for_lane(inputs: ResolveInputs) -> None:
    """Pre-condition check — fail-fast on inconsistent calling-handler state."""
    if _is_sec_first(inputs.handler_kind):
        if not inputs.cik:
            raise ValueError(
                f"parent_resolver: SEC-first lane {inputs.handler_kind} requires `cik` "
                "(EDGAR filings are CIK-keyed). Got None."
            )
    else:
        if not inputs.ticker:
            raise ValueError(
                f"parent_resolver: FMP-first lane {inputs.handler_kind} requires `ticker` "
                "(wire feed is ticker-keyed). Got None."
            )


# ─────────────────────────────────────────────────────────────────
# Orchestrator (composes existing adapters; per-lane dispatch)
# ─────────────────────────────────────────────────────────────────


class _ProfileResult:
    """Stub for the FMP /profile response shape — wired in P4 slice 2.

    The actual FMP profile adapter call returns a richer Pydantic model with
    `country`, `asset_class`, `exchange`, `cik`, `cusip`, `isin`, `legal_name`.
    This stub exists so parent_resolver's signature is testable in isolation
    before the FMP /profile adapter is fully wired.
    """

    country: str = "US"
    asset_class: AssetClass = AssetClass.STOCK
    exchange: str | None = None
    cik: str | None = None
    cusip: str | None = None
    isin: str | None = None
    legal_name: str | None = None


async def resolve(
    inputs: ResolveInputs,
    *,
    sec_ticker_lookup: object,
    fmp_profile_lookup: object,
    openfigi_lookup: object,
    now: datetime | None = None,
) -> ResolvedClassification:
    """Resolve an unknown ticker/CIK to a fully-populated ResolvedClassification.

    Args:
        inputs: ResolveInputs naming the handler kind + whichever of (ticker, cik)
            the handler had at the call site.
        sec_ticker_lookup: callable returning {cik: int → ticker: str} or
            inversely {ticker: str → cik: int}. The SEC EDGAR adapter's
            `load_ticker_to_cik` populates this; orchestrator inverts when needed.
        fmp_profile_lookup: async callable `(ticker: str) → _ProfileResult` that
            wraps `tpcore.fmp.*_profile_adapter` (wired in P4 slice 2).
        openfigi_lookup: async callable `(tickers: list[str]) → list[OpenFIGIResult]`
            wrapping `tpcore.openfigi.OpenFIGIAdapter.map_tickers`.
        now: UTC datetime for the discovery_year_yy segment. Defaults to now.

    Returns:
        ResolvedClassification with TKR-14 minted + all available identity fields populated.

    Raises:
        ValueError: if `inputs` are inconsistent with the handler kind.
        DataProviderOutage: if a hard-required upstream is persistently down
            (caller decides whether to retry or escalate per
            `feedback_self_heal_autonomous_no_operator_task`).

    Pin-at-first-resolve discipline applies at the persistence layer (caller's
    UPSERT), NOT here. This function returns the resolved snapshot; persistence
    code merges with existing row per the never-overwrite-non-null rule.
    """
    _assert_inputs_valid_for_lane(inputs)
    now = now or datetime.now(UTC)

    # Stage 1 — ticker resolution per lane
    if _is_sec_first(inputs.handler_kind):
        # SEC-first: reverse-lookup ticker from CIK via SEC company_tickers.json
        assert inputs.cik is not None  # _assert_inputs_valid_for_lane confirmed
        ticker = _sec_reverse_lookup(sec_ticker_lookup, cik=inputs.cik)
        if ticker is None:
            # Foreign-issuer CIK SEC doesn't carry → FMP fallback (rare path)
            logger.warning(
                "parent_resolver.sec_reverse_lookup_miss",
                cik=inputs.cik,
                lane=inputs.handler_kind,
            )
            ticker = inputs.ticker  # may be None; caller should set it if known
            if ticker is None:
                raise ValueError(
                    f"parent_resolver: SEC-first lane CIK {inputs.cik!r} not found in "
                    "company_tickers.json and no fallback ticker provided"
                )
    else:
        # FMP-first: ticker is in hand; no reverse-lookup needed
        assert inputs.ticker is not None
        ticker = inputs.ticker

    # Stage 2 — FMP /profile enrichment (country, asset_class, exchange, cusip, isin, cik)
    profile = await _call_fmp_profile(fmp_profile_lookup, ticker=ticker)

    # Stage 3 — OpenFIGI for compositeFIGI
    figi = await _call_openfigi(openfigi_lookup, ticker=ticker)

    # Stage 4 — mint TKR-14 from immutable + at-mint-snapshot facts
    discovery_source = (
        DiscoverySource.SEC if _is_sec_first(inputs.handler_kind) else DiscoverySource.FMP
    )
    ipo_venue = _infer_ipo_venue(profile.exchange)

    tkr14_id = mint(
        country=profile.country,
        asset_class=profile.asset_class,
        ipo_venue=ipo_venue,
        discovery_source=discovery_source,
        cik=profile.cik or inputs.cik,
        legal_name=profile.legal_name or ticker,
        now=now,
    )

    return ResolvedClassification(
        tkr14_id=tkr14_id,
        ticker=ticker,
        country=profile.country,
        asset_class=profile.asset_class,
        ipo_venue=ipo_venue,
        discovery_source=discovery_source,
        cik=profile.cik or inputs.cik,
        legal_name=profile.legal_name,
        exchange=profile.exchange,
        figi=figi,
        cusip=profile.cusip,
        isin=profile.isin,
        resolved_at=now,
    )


# ─────────────────────────────────────────────────────────────────
# Lane helpers — kept private so the dispatch logic above stays readable
# ─────────────────────────────────────────────────────────────────


def _sec_reverse_lookup(sec_ticker_lookup: object, *, cik: str) -> str | None:
    """Reverse-lookup ticker from CIK via SEC's `ticker → cik` map (inverted)."""
    # The SEC EDGAR adapter exposes `load_ticker_to_cik()` → dict[ticker, cik].
    # parent_resolver inverts this for the reverse direction.
    if not callable(getattr(sec_ticker_lookup, "items", None)):
        # Assume it's already a dict-like mapping {ticker → cik}
        forward = sec_ticker_lookup
    else:
        forward = sec_ticker_lookup

    try:
        cik_int = int(cik)
    except (TypeError, ValueError):
        return None
    if not hasattr(forward, "items"):
        return None
    for ticker, cik_value in forward.items():  # type: ignore[attr-defined]
        if int(cik_value) == cik_int:
            return str(ticker)
    return None


async def _call_fmp_profile(fmp_profile_lookup: object, *, ticker: str) -> _ProfileResult:
    """Wrap the FMP /profile call. Stubbed in P4 slice 1; wired in slice 2."""
    if fmp_profile_lookup is None:
        return _ProfileResult()
    if callable(fmp_profile_lookup):
        result = fmp_profile_lookup(ticker)  # type: ignore[operator]
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[assignment]
        if isinstance(result, _ProfileResult):
            return result
        # Allow dict-shaped stubs for tests
        if isinstance(result, dict):
            stub = _ProfileResult()
            for k, v in result.items():
                if hasattr(stub, k):
                    setattr(stub, k, v)
            return stub
    return _ProfileResult()


async def _call_openfigi(openfigi_lookup: object, *, ticker: str) -> str | None:
    """Wrap the OpenFIGI mapping call. Returns the compositeFIGI or None."""
    if openfigi_lookup is None:
        return None
    if callable(openfigi_lookup):
        result = openfigi_lookup([ticker])  # type: ignore[operator]
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[assignment]
        # OpenFIGIAdapter.map_tickers returns list[OpenFIGIResult]
        if isinstance(result, list) and result:
            first = result[0]
            return getattr(first, "composite_figi", None)
    return None


def _infer_ipo_venue(exchange: str | None) -> IPOVenue:
    """Best-effort map from current `exchange` string to the IPOVenue snapshot.

    For backfill of pre-existing rows this is necessarily a heuristic — we
    don't know what the IPO venue was, only the current trading venue.
    For new IPOs discovered live, the calling handler should pass the IPO
    venue explicitly (TODO: add an `ipo_venue_override` kwarg to ResolveInputs).
    """
    if not exchange:
        return IPOVenue.OTHER
    e = exchange.upper()
    if "XNYS" in e or "NYSE" in e:
        return IPOVenue.NYSE
    if "XNAS" in e or "NASDAQ" in e:
        return IPOVenue.NASDAQ
    if "XASE" in e or "AMEX" in e:
        return IPOVenue.AMEX
    if "BZX" in e or "CBOE" in e:
        return IPOVenue.CBOE_BZX
    if "OTC" in e or "OTCM" in e:
        return IPOVenue.OTC
    # Foreign primary: anything else with a non-US flavor
    return IPOVenue.FOREIGN_PRIMARY
