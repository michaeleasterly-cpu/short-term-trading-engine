"""Survivorship-free, identity-first universe assembler + TKR-14 minter.

Plan 3 Phase 1 (identity-first re-ingest). Spec:
``docs/superpowers/specs/2026-06-04-data-layer-rebuild-design.md`` §4 / §5.2 /
§5.3 / §5.5. Discovery: ``docs/audits/2026-06-05-identity-build-code-state.md``.

This is the **pure** half of the ``universe_build`` stage — no DB, no
network, no asyncpg. It takes already-fetched source rosters (SEC full
company list + per-CIK FPFD, FMP symbol list, FMP delisted/symbol-change
history) and produces the survivorship-free set of
``UniverseSecurity`` rows, each carrying a minted TKR-14 ``id`` and an
explicit ``lifetime_start`` (NEVER the ``1900-01-01`` sentinel).

No-sentinel invariant (A6 — review #5): migration ``20260604_0600`` ALREADY
DROPPED the ``'1900-01-01'`` column DEFAULT on live (verified), so the
``lifetime_start`` column is ``NOT NULL`` with NO default. The invariant
therefore rests on TWO structural facts — (a) the no-default column (a row
inserted without an explicit ``lifetime_start`` ERRORS rather than silently
sentineling), and (b) the in-stage refuse-to-write guard in
``_stage_universe_build`` (raises if any assembled row carries the sentinel).
``resolve_lifetime_start`` never emits the sentinel in the first place, so all
three layers agree. A DB-level CHECK is optional and out of scope here.

The stage handler in ``scripts/ops.py`` owns the I/O (CSV-first fetch
with ``tpcore.outage.with_retry``, chunked INSERT) and calls these pure
functions — mirroring the engine/data symmetry the codebase favours.

Why this exists (discovery §1/§3): the legacy minter
``tpcore/data/classify_tickers.py`` sources Alpaca active-only
(survivorship-VIOLATING) and never writes ``id`` / ``cik`` /
``lifetime_start`` / FPFD. This module is the correct SEC-first,
FPFD-anchored, delisted-inclusive replacement.

Source authority (spec §5.2 / A7 / A8; identity-path rule §3):
  * SEC EDGAR is authoritative for US CIK-backed issuers — discovery
    source ``S``, ``cik`` set, ``lifetime_start = FPFD``.
  * FMP is the gray-zone fallback ONLY (spec OQ-1): an FMP-listed symbol
    with no SEC CIK is minted with ``cik=None``, ``discovery_source='F'``,
    ``lifetime_start = earliest FMP date``. FMP never overrides a SEC
    identity for a ticker SEC also covers.
"""
from __future__ import annotations

from datetime import date, datetime

import structlog
from pydantic import BaseModel, ConfigDict, Field

from tpcore.identity.tkr14 import (
    AssetClass,
    DiscoverySource,
    IPOVenue,
    mint,
)

logger = structlog.get_logger(__name__)


# Birthday-paradox at ~25-bit hash + tens of thousands of issuers makes a
# salt-collision retry necessary; the mint docstring documents salt=1,2,…
# The cap is generous — a run that needs >50 salts on one issuer is a
# pathology worth surfacing, not silently looping forever.
_MAX_SALT_RETRIES: int = 50


class SECUniverseEntry(BaseModel):
    """One issuer from the SEC full company list / submissions index.

    Sourced from ``company_tickers.json`` (ticker→CIK→title) joined to
    the per-CIK FPFD computed by ``SECCompanyFactsAdapter`` (the fixed
    earliest-``filingDate`` value — spec §5.5/A5).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    cik: str
    """Zero-padded 10-digit CIK string."""
    legal_name: str | None = None
    first_public_filing_date: date | None = None
    """FPFD = earliest filingDate across the full submission index. When
    None (issuer present in company_tickers but no submissions row) the
    assembler falls back to the FMP earliest date, then ``now`` — but
    NEVER the sentinel."""


class FMPUniverseEntry(BaseModel):
    """One symbol from the FMP symbol list / delisted history.

    The FMP-only gray zone (spec OQ-1): a US-listed micro-cap or a
    non-US symbol that SEC's company_tickers does not carry. Minted with
    ``cik=None`` + ``discovery_source='F'``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    company_name: str | None = None
    earliest_date: date | None = None
    """Earliest date FMP has for the symbol (first bar / IPO date proxy).
    Used as ``lifetime_start`` for FMP-only securities (spec OQ-1)."""
    delisted: bool = False
    delisting_date: date | None = None
    """FMP delisting date → ``lifetime_end`` for the survivorship-free
    roster (invariant G1/G3 — delisted tickers are INCLUDED)."""
    country: str = "US"
    """ISO 3166-1 alpha-2 (FMP profile country). Defaults to US."""


class UniverseSecurity(BaseModel):
    """One minted universe security ready to INSERT into
    ``platform.ticker_classifications``.

    ``lifetime_start`` is ALWAYS populated (NOT NULL, no sentinel — spec
    §3.1/A6). ``lifetime_end`` is set only for delisted securities.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(..., description="Minted TKR-14 identifier (PK).")
    ticker: str = Field(..., description="ON CONFLICT UPSERT key (survivor).")
    current_ticker: str = Field(..., description="Canonical mutable symbol.")
    asset_class: str
    source: str = Field(..., description="'sec' | 'fmp' provenance tag.")
    cik: str | None = None
    legal_name: str | None = None
    lifetime_start: date
    lifetime_end: date | None = None
    discovery_source: str = Field(
        ..., description="TKR-14 pos-7 snapshot: 'S' (SEC) | 'F' (FMP)."
    )


def resolve_lifetime_start(
    *,
    fpfd: date | None,
    fmp_earliest: date | None,
    now: datetime,
) -> date:
    """Resolve ``lifetime_start`` with NO sentinel fallback (spec §3.1/A6).

    Precedence: SEC FPFD → FMP earliest date → ``now`` (mint date). The
    ``1900-01-01`` sentinel is FORBIDDEN — every row gets a real,
    source-anchored date. ``now`` is the honest last-resort floor (the
    security exists today; we have no earlier evidence) and is still a
    real date, never the sentinel.
    """
    if fpfd is not None:
        return fpfd
    if fmp_earliest is not None:
        return fmp_earliest
    return now.date()


def _guarded_lifetime_end(
    *,
    delisting_date: date | None,
    lifetime_start: date,
    ticker: str,
) -> date | None:
    """Return a ``lifetime_end`` only when it is strictly after
    ``lifetime_start`` (review #5).

    The ``tc_lifetime_order`` CHECK (``lifetime_end IS NULL OR lifetime_end >
    lifetime_start``) rejects the WHOLE chunk on a single dirty vendor row.
    A dirty FMP ``delisting_date`` that is on or before ``lifetime_start``
    (stale/garbled vendor data) is DROPPED here (+ WARN) so one bad row can
    never poison the batch. Dropping ``lifetime_end`` keeps the security in
    the survivorship-free roster as still-active rather than failing the load.
    """
    if delisting_date is None:
        return None
    if delisting_date <= lifetime_start:
        logger.warning(
            "universe_build.bad_delisting_date_dropped",
            ticker=ticker,
            delisting_date=delisting_date.isoformat(),
            lifetime_start=lifetime_start.isoformat(),
        )
        return None
    return delisting_date


def _asset_class_for(legal_name: str | None) -> AssetClass:
    """Coarse asset-class classification for the TKR-14 pos-3 segment.

    Universe-build defaults to STOCK; the existing ``classify_tickers``
    name-heuristic + the later OpenFIGI ``reclassify_asset_class`` stage
    refine ETF/fund/etc. on the minted rows. Keeping this coarse avoids
    re-implementing the ETF regex here (no duplication — discovery §1).
    """
    del legal_name  # coarse default; refinement is a downstream stage
    return AssetClass.STOCK


def mint_with_salt_retry(
    *,
    country: str,
    asset_class: AssetClass,
    ipo_venue: IPOVenue,
    discovery_source: DiscoverySource,
    cik: str | None,
    legal_name: str,
    now: datetime,
    seen_ids: set[str],
) -> str:
    """Mint a TKR-14 id, retrying with an incrementing salt on collision.

    The ``mint`` docstring documents the salt-retry loop: a 25-bit issuer
    hash has a non-trivial birthday-paradox collision probability across
    tens of thousands of issuers. Each salt increment changes ALL bits of
    the SHA-1 digest, so a collision typically resolves on the first
    retry. ``seen_ids`` accumulates every id minted in THIS run so the
    in-run uniqueness is guaranteed before any DB write.

    Raises ``RuntimeError`` if ``_MAX_SALT_RETRIES`` is exhausted — a
    pathology worth surfacing, not silently looping.
    """
    for salt in range(_MAX_SALT_RETRIES + 1):
        candidate = mint(
            country=country,
            asset_class=asset_class,
            ipo_venue=ipo_venue,
            discovery_source=discovery_source,
            cik=cik,
            legal_name=legal_name,
            now=now,
            salt=salt,
        )
        if candidate not in seen_ids:
            seen_ids.add(candidate)
            return candidate
        logger.warning(
            "universe_build.mint_collision_retry",
            colliding_id=candidate,
            salt=salt,
            cik=cik,
        )
    raise RuntimeError(
        f"universe_build: TKR-14 mint collision unresolved after "
        f"{_MAX_SALT_RETRIES} salt retries (cik={cik!r}, "
        f"legal_name={legal_name!r}) — surfacing rather than looping."
    )


def assemble_universe(
    *,
    sec_entries: list[SECUniverseEntry],
    fmp_entries: list[FMPUniverseEntry],
    now: datetime,
) -> list[UniverseSecurity]:
    """Assemble the survivorship-free universe = SEC ∪ FMP, SEC-first.

    Pure function: no I/O. Given the two source rosters (already fetched
    by the stage handler), produce one ``UniverseSecurity`` per security,
    each with a minted TKR-14 ``id`` + an explicit ``lifetime_start``.

    Authority (spec §5.2/A7/A8): SEC is authoritative for any ticker it
    covers. An FMP entry whose ticker SEC also carries is DROPPED from
    the FMP leg (SEC wins identity; FMP does NOT override) — its delisting
    metadata is still applied to the SEC-minted row. An FMP-only ticker
    (no SEC CIK) is minted from FMP with ``cik=None``,
    ``discovery_source='F'`` (the OQ-1 gray-zone rule).

    Survivorship-freeness (invariant G1/G3): delisted tickers are
    INCLUDED, with ``lifetime_end`` set from the FMP delisting date.
    """
    seen_ids: set[str] = set()
    out: list[UniverseSecurity] = []

    # Index FMP by ticker so SEC entries can adopt FMP delisting metadata,
    # and FMP-only tickers (SEC miss) can be identified.
    fmp_by_ticker: dict[str, FMPUniverseEntry] = {}
    for fe in fmp_entries:
        t = fe.ticker.strip().upper()
        if not t:
            continue
        # Last-write-wins on duplicate FMP rows is acceptable — the
        # delisting metadata is what we care about; a later row with a
        # delisting date is preferred.
        existing = fmp_by_ticker.get(t)
        if existing is None or (fe.delisted and not existing.delisted):
            fmp_by_ticker[t] = fe

    sec_tickers: set[str] = set()

    # ── 1. SEC leg (authoritative) ──────────────────────────────────
    for se in sec_entries:
        ticker = se.ticker.strip().upper()
        cik = (se.cik or "").strip() or None
        if not ticker or not cik:
            # SEC entry with no ticker/CIK is unusable for identity.
            continue
        sec_tickers.add(ticker)
        fmp_match = fmp_by_ticker.get(ticker)
        fmp_earliest = fmp_match.earliest_date if fmp_match else None
        lifetime_start = resolve_lifetime_start(
            fpfd=se.first_public_filing_date,
            fmp_earliest=fmp_earliest,
            now=now,
        )
        # Delisting metadata from FMP applies to the SEC-minted row
        # (survivorship-free): an SEC issuer that FMP marks delisted gets
        # a lifetime_end. SEC Form 25 boundary is a later enrichment.
        lifetime_end: date | None = None
        if fmp_match is not None and fmp_match.delisted:
            lifetime_end = _guarded_lifetime_end(
                delisting_date=fmp_match.delisting_date,
                lifetime_start=lifetime_start,
                ticker=ticker,
            )
        ac = _asset_class_for(se.legal_name)
        legal_name = (se.legal_name or "").strip() or ticker
        new_id = mint_with_salt_retry(
            country="US",
            asset_class=ac,
            # Historical IPO venue unknown at universe-build — 'O' (other)
            # honest snapshot; refined by later enrichment if ever wanted.
            ipo_venue=IPOVenue.OTHER,
            discovery_source=DiscoverySource.SEC,
            cik=cik,
            legal_name=legal_name,
            now=now,
            seen_ids=seen_ids,
        )
        out.append(
            UniverseSecurity(
                id=new_id,
                ticker=ticker,
                current_ticker=ticker,
                asset_class=ac.name.lower(),
                source="sec",
                cik=cik,
                legal_name=se.legal_name,
                lifetime_start=lifetime_start,
                lifetime_end=lifetime_end,
                discovery_source=DiscoverySource.SEC.value,
            )
        )

    # ── 2. FMP-only leg (gray-zone fallback; OQ-1) ──────────────────
    for ticker, fe in sorted(fmp_by_ticker.items()):
        if ticker in sec_tickers:
            # SEC covers it — FMP must NOT override identity (A8). Already
            # minted in the SEC leg; FMP delisting metadata applied there.
            continue
        country = (fe.country or "US").strip().upper()
        if len(country) != 2 or not country.isalpha():
            country = "US"
        lifetime_start = resolve_lifetime_start(
            fpfd=None,
            fmp_earliest=fe.earliest_date,
            now=now,
        )
        lifetime_end = (
            _guarded_lifetime_end(
                delisting_date=fe.delisting_date,
                lifetime_start=lifetime_start,
                ticker=ticker,
            )
            if fe.delisted
            else None
        )
        ac = _asset_class_for(fe.company_name)
        legal_name = (fe.company_name or "").strip() or ticker
        new_id = mint_with_salt_retry(
            country=country,
            asset_class=ac,
            ipo_venue=IPOVenue.OTHER,
            discovery_source=DiscoverySource.FMP,
            cik=None,
            legal_name=legal_name,
            now=now,
            seen_ids=seen_ids,
        )
        out.append(
            UniverseSecurity(
                id=new_id,
                ticker=ticker,
                current_ticker=ticker,
                asset_class=ac.name.lower(),
                source="fmp",
                cik=None,
                legal_name=fe.company_name,
                lifetime_start=lifetime_start,
                lifetime_end=lifetime_end,
                discovery_source=DiscoverySource.FMP.value,
            )
        )

    logger.info(
        "universe_build.assembled",
        n_total=len(out),
        n_sec=sum(1 for s in out if s.source == "sec"),
        n_fmp_only=sum(1 for s in out if s.source == "fmp"),
        n_delisted=sum(1 for s in out if s.lifetime_end is not None),
    )
    return out


__all__ = [
    "FMPUniverseEntry",
    "SECUniverseEntry",
    "UniverseSecurity",
    "assemble_universe",
    "mint_with_salt_retry",
    "resolve_lifetime_start",
]
