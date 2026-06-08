"""Clean-slate identity-spine minter — the staging-schema rebuild (Phase A).

Plan: ``docs/superpowers/plans/2026-06-08-data-foundation-reingest-plan.md``
(REVISED 2026-06-08 — "square one includes the spine"). Spec:
``docs/superpowers/specs/2026-06-08-data-foundation-systemic-fix-design.md``
§0 (re-ingest-from-scratch, contract-first) + §7-B (SEC-first build).

This is the **pure** half of the clean-slate staging build — no DB, no
network, no asyncpg. The orchestrator (``scripts/stage_spine_build.py``) owns
the I/O: it reads the cached SEC ``submissions.zip`` + the live
``platform.prices_daily`` per-symbol bar spans + FMP/known-delisting
corroboration, builds the ``SpineBuildInput`` rows, and calls
``assemble_spine`` here to mint the clean ``ticker_classifications`` set.

Why a NEW module instead of patching ``universe_build`` (discovery §1):
``universe_build`` is the ADDITIVE minter (``ON CONFLICT DO NOTHING``, no
unique index on cik, single ``current_ticker``-keyed window) the audit found
cannot reconcile the dirty live spine (1,046 multi-CIK, 20 dup ticker+CIK
SPAC tangles, 5,164 NULL ``current_ticker``, 5,080 synthetic Jan-1 starts,
overlapping FB/META/FISV windows). This module is the clean-slate replacement:
it mints ONE classification per real (entity, ticker-era), with windows
constructed to be cross-entity disjoint AND to cover the EXACT live price-bar
span of every priced symbol (the P3 make-it-work guarantee). It REUSES the
existing pure layers (``derive_ticker_history``, ``assemble_issuer``,
``derive_issuer_securities``, ``tkr14.mint``) — symmetry, not copy.

The four build rules baked in (the operator's "bake correctness into the
mint, clean-slate not refine" directive):

  * **One classification per real entity-era.** SoT = SEC submissions
    (``tickers[]`` for the live symbol set per CIK, ``formerNames[]`` for the
    legal-name eras, FPFD) + the survivorship-free price snapshot (first/last
    bar per symbol) + FMP listing/delisting + ``KNOWN_DELISTINGS``.
  * **lifetime_start** = SEC FPFD primary, refined DOWN by the earliest
    real-day price-bar / FMP evidence (a security can trade before its first
    SEC filing — foreign / OTC / pre-IPO — so the earliest real-day evidence
    wins when it is EARLIER than FPFD). SEC wins on a real-day conflict in the
    OTHER direction (FPFD earlier than any bar). NEVER a synthetic Jan-1.
  * **lifetime_end** = SEC Form-25/15 → FMP delisting → ``KNOWN_DELISTINGS``;
    dropped if ≤ start; extended to cover the last attributed bar if a bar
    falls after the evidence date (a real bar is harder evidence than a vendor
    delisting date that predates it).
  * **SPAC unit/warrant/share-class collapse.** ``tickers[]`` lists every live
    symbol of one CIK (HCAC/HCACU/HCACR). Pure share-class/unit/warrant symbol
    variants of the SAME entity are NOT minted as 7 Jan-1 dups — each symbol
    that carries price bars gets ONE classification (so its bars resolve), all
    sharing the CIK / issuer (the M:N fan-out), with a single SEC-anchored
    window. Symbols of the CIK with NO price bars are dropped (no windowless
    rows).

The disjointness + coverage guarantees are proven by the staging gate
(``tpcore/identity/staging_gate.py``) probes P1-P5, which dry-run the resolver
against the EXACT live bars the re-ingest will write.
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

# Same salt cap as universe_build — a run needing >50 salts on one
# (cik, asset_class, year) tuple is a pathology worth surfacing.
_MAX_SALT_RETRIES: int = 50

# Asset-class string → TKR-14 AssetClass enum (pos-3 segment). The spine's
# asset_class column is the lower-case string; the mint needs the enum.
_ASSET_CLASS_ENUM: dict[str, AssetClass] = {
    "stock": AssetClass.STOCK,
    "preferred": AssetClass.PREFERRED,
    "etf": AssetClass.ETF,
    "etn": AssetClass.NOTE,
    "fund": AssetClass.FUND,
    "reit": AssetClass.REIT,
    "trust": AssetClass.TRUST,
    "adr": AssetClass.ADR,
    "spac": AssetClass.SPAC_UNIT,
    "warrant": AssetClass.WARRANT,
    "note": AssetClass.NOTE,
}


class PriceBarSpan(BaseModel):
    """Observed price-bar span for one symbol in ``platform.prices_daily``.

    This is the BINDING P3 constraint: the staged window for ``ticker`` must
    span ``[min_date, max_date]`` so the re-ingest's resolver attributes every
    one of these bars. The bars are keyed by the literal symbol string as it
    appears in ``prices_daily.ticker`` (the survivorship-free snapshot keys
    inconsistently — META carries Facebook-era bars under 'META', FISV carries
    post-rename bars under 'FISV' — and the resolver matches the literal
    string, so the window must cover the span of whatever symbol the bars
    actually live under)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    min_date: date
    max_date: date
    n_bars: int = Field(ge=1)


class SpineBuildInput(BaseModel):
    """One (entity, ticker-era) build input assembled by the orchestrator.

    The orchestrator resolves identity SEC-first: a symbol present in SEC
    ``tickers[]`` is SEC-backed (``cik`` set, ``discovery_source='S'``);
    otherwise FMP/price-evidence fallback (``cik=None``, ``'F'``). All date
    evidence is carried as explicit optional fields so ``assemble_spine`` can
    apply the precedence rules without re-reading any source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    """The symbol this classification carries (the survivor / current symbol
    for an active entity; the era symbol for a delisted era)."""
    asset_class: str
    cik: str | None = None
    legal_name: str | None = None
    country: str = "US"
    discovery_source: str = "F"
    """'S' (SEC tickers[] match) | 'F' (FMP / price-evidence fallback)."""
    asset_class_verified: bool = False
    """True when ``asset_class`` is SEC-document-type or SIC authoritative."""

    # ── Date evidence (the orchestrator supplies whatever it has) ──────────
    fpfd: date | None = None
    """SEC first-public-filing-date (earliest filingDate across the merged
    submissions). The primary ``lifetime_start`` anchor."""
    first_bar: date | None = None
    last_bar: date | None = None
    """Earliest / latest bar attributed to THIS symbol in prices_daily."""
    fmp_earliest: date | None = None
    """Earliest FMP-listed date (corroboration / fallback start)."""
    sec_delisting_date: date | None = None
    """SEC Form-25/15 effective date (authoritative lifetime_end)."""
    fmp_delisting_date: date | None = None
    """FMP delisting date (fallback lifetime_end)."""
    known_delisting_date: date | None = None
    """KNOWN_DELISTINGS curated date (last-resort lifetime_end)."""
    still_trading: bool = False
    """True when the symbol's max bar is within the active recency window OR
    the entity is a current SEC filer — the window stays open (valid_to NULL)."""


class SpineSecurity(BaseModel):
    """One minted clean-slate classification ready for the staging table.

    ``lifetime_start`` is ALWAYS a real, source-anchored date (no Jan-1
    synthetic). ``current_ticker`` is ALWAYS populated (the reuse-build keys
    on it; a NULL would drop the row from ticker_history)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    ticker: str
    current_ticker: str
    asset_class: str
    source: str
    cik: str | None
    legal_name: str | None
    lifetime_start: date
    lifetime_end: date | None
    discovery_source: str
    metadata_source: str
    asset_class_verified: bool
    first_public_filing_date: date | None = None
    """The SEC FPFD as supplied (the true earliest filing date) — distinct
    from ``lifetime_start`` (which may be refined earlier by a price bar).
    Carried so the staged spine's ``first_public_filing_date`` column holds
    the SEC anchor, not the refined start."""


def _is_jan1(d: date | None) -> bool:
    """A Jan-1 date is the synthetic-placeholder class the dirty live spine
    used (5,080 rows) — treated as NON-real-day evidence here (plan A1, P4)."""
    return d is not None and d.month == 1 and d.day == 1


def resolve_lifetime_start(
    *,
    fpfd: date | None,
    first_bar: date | None,
    fmp_earliest: date | None,
    now: datetime,
) -> date:
    """Resolve ``lifetime_start`` — SEC FPFD primary, refined DOWN by the
    earliest REAL-DAY evidence; NEVER a synthetic Jan-1 when real-day
    corroboration exists.

    Precedence + refinement (spec §7-B; plan A1; P4):
      * The candidate anchors are FPFD, the first real price bar, and the FMP
        earliest date.
      * A Jan-1 candidate is the synthetic-placeholder class the dirty live
        spine carried (5,080 rows); it is NOT a real-day anchor. The FMP
        earliest is harvested from the live spine, so a Jan-1 ``fmp_earliest``
        (or, defensively, a Jan-1 FPFD) is DROPPED from the real-day set when
        any non-Jan-1 anchor exists — otherwise the dirty Jan-1 of a DIFFERENT
        entity-era would win the ``min()`` and re-introduce the exact rot the
        rebuild removes (e.g. ABR's old delisted-era Jan-1 leaking onto the
        current ABR).
      * Among the real-day anchors we take the EARLIEST: a security can
        legitimately TRADE before its first SEC filing (foreign / OTC /
        pre-IPO), so a bar earlier than FPFD wins; FPFD wins when earlier than
        any bar. This guarantees ``lifetime_start <= first_bar`` (the P3
        lower-bound) by construction whenever a bar exists.
      * If the ONLY evidence is a Jan-1 (a genuine no-other-evidence case), it
        is used (a real, if coarse, date — still never the 1900 sentinel).
      * ``now`` is the honest last-resort floor (no evidence at all).
    """
    all_anchors = [d for d in (fpfd, first_bar, fmp_earliest) if d is not None]
    real_day = [d for d in all_anchors if not _is_jan1(d)]
    if real_day:
        return min(real_day)
    if all_anchors:
        return min(all_anchors)  # only Jan-1 evidence — genuine, use it
    return now.date()


def _next_day(d: date) -> date:
    return date.fromordinal(d.toordinal() + 1)


def resolve_lifetime_end(
    *,
    sec_delisting_date: date | None,
    fmp_delisting_date: date | None,
    known_delisting_date: date | None,
    last_bar: date | None,
    lifetime_start: date,
    still_trading: bool,
    ticker: str,
) -> date | None:
    """Resolve ``lifetime_end`` — SEC Form-25/15 → FMP delisting →
    ``KNOWN_DELISTINGS``; open when still trading; never ≤ start.

    Precedence (spec §7-B; plan A2):
      * ``still_trading`` ⇒ open window (``None``) — the entity is current.
      * else the first available of (SEC, FMP, KNOWN) delisting date.
      * **Bar-coverage extension (P3):** a real attributed bar AFTER the
        evidence date is harder evidence the security still traded then, so
        the end is extended past the last bar (half-open: ``valid_to`` is
        exclusive, so a bar on ``last_bar`` needs ``valid_to > last_bar``).
        A vendor delisting date that predates a real bar is stale.
      * Dropped (``None``) if the resolved end is ≤ ``lifetime_start`` (a
        garbled vendor date) — the security stays in the survivorship-free
        roster as still-active rather than failing the date-order CHECK.
    """
    if still_trading:
        return None
    candidates = [
        d
        for d in (sec_delisting_date, fmp_delisting_date, known_delisting_date)
        if d is not None
    ]
    end: date | None = min(candidates) if candidates else None
    if last_bar is not None and (end is None or end <= last_bar):
        end = _next_day(last_bar)
    if end is None:
        return None
    if end <= lifetime_start:
        logger.warning(
            "staging_spine.bad_lifetime_end_dropped",
            ticker=ticker,
            lifetime_end=end.isoformat(),
            lifetime_start=lifetime_start.isoformat(),
        )
        return None
    return end


def _asset_class_enum(asset_class: str) -> AssetClass:
    """Map the spine asset_class string to the TKR-14 pos-3 enum (coarse
    fallback to STOCK for an unknown string — surfaced via WARN)."""
    ac = _ASSET_CLASS_ENUM.get((asset_class or "").strip().lower())
    if ac is None:
        logger.warning("staging_spine.unknown_asset_class", asset_class=asset_class)
        return AssetClass.STOCK
    return ac


def mint_era_id(
    *,
    inp: SpineBuildInput,
    now: datetime,
    seen_ids: set[str],
) -> str:
    """Mint a TKR-14 id for one (entity, ticker-era), salt-retrying on
    collision.

    **Per-symbol identity seed (clean-slate fix).** ``tkr14.mint`` seeds the
    issuer hash on ``country|cik`` when a CIK is present, IGNORING the ticker
    and legal_name. The legacy ``universe_build`` minted ONE row per CIK
    (current_ticker), so distinct securities of one CIK never collided. The
    clean-slate mint emits ONE row per priced SYMBOL, so an ETF trust / bank
    holding-co CIK with dozens of symbols (e.g. an iShares trust CIK attached
    to many ETF symbols in the live evidence) all want the SAME 25-bit hash at
    salt=0 — and salt-retry exhausts in the 25-bit space once >50 siblings
    share the prefix. The structural fix: seed the hash on the SECURITY
    identity (``cik|ticker``) via the legal_name fallback (``cik=None`` at
    MINT time only). Each distinct symbol therefore maps DETERMINISTICALLY to a
    distinct id (a re-run reproduces it), while the real CIK is preserved on the
    output ``cik`` COLUMN (the id is a stable unique key, not the CIK store —
    the CIK linkage lives in the column + ``issuer_securities``, not pos-7 of
    the id). The salt-retry remains a thin guard for the rare residual 25-bit
    birthday collision ACROSS different (cik, ticker) seeds.
    """
    ac = _asset_class_enum(inp.asset_class)
    country = (inp.country or "US").strip().upper()
    if len(country) != 2 or not country.isalpha():
        country = "US"
    src = (
        DiscoverySource.SEC
        if inp.discovery_source == "S"
        else DiscoverySource.FMP
    )
    # Security-identity seed: cik|ticker (or legal_name|ticker for cik-NULL).
    # Fed through the legal_name fallback with cik=None so mint hashes the
    # COMPOSITE, giving each symbol of a shared CIK a distinct deterministic id.
    base = inp.cik or (inp.legal_name or "").strip() or "NA"
    seed_name = f"{base}|{inp.ticker.strip().upper()}"
    for salt in range(_MAX_SALT_RETRIES + 1):
        candidate = mint(
            country=country,
            asset_class=ac,
            ipo_venue=IPOVenue.OTHER,
            discovery_source=src,
            cik=None,
            legal_name=seed_name,
            now=now,
            salt=salt,
        )
        if candidate not in seen_ids:
            seen_ids.add(candidate)
            return candidate
        # DEBUG (not WARN): a 25-bit-hash birthday collision across distinct
        # (cik|ticker) seeds is EXPECTED at universe scale (~12k rows) and
        # resolves at salt=1-2; WARN-logging each one floods the run (it
        # turned a 0.1s mint into a 20-min I/O spin). The unresolved-after-cap
        # case still RAISES below — that is the real pathology to surface.
        logger.debug(
            "staging_spine.mint_collision_retry",
            colliding_id=candidate,
            salt=salt,
            cik=inp.cik,
            ticker=inp.ticker,
        )
    raise RuntimeError(
        "staging_spine: TKR-14 mint collision unresolved after "
        f"{_MAX_SALT_RETRIES} salt retries (cik={inp.cik!r}, "
        f"ticker={inp.ticker!r}) — surfacing rather than looping."
    )


def assemble_spine(
    inputs: list[SpineBuildInput],
    *,
    now: datetime,
) -> list[SpineSecurity]:
    """Assemble the clean-slate ``ticker_classifications`` set.

    Pure function: no I/O. One ``SpineSecurity`` per ``SpineBuildInput``
    (one per real (entity, ticker-era)). Applies the lifetime-start /
    lifetime-end precedence + the no-sentinel + always-``current_ticker``
    invariants. The orchestrator has already done the SEC-first identity
    resolution + the SPAC-variant collapse (one input per priced symbol of a
    CIK); this layer is the deterministic mint + date-resolution.

    The window per symbol is constructed to COVER that symbol's price-bar span
    by construction (``lifetime_start <= first_bar`` via the earliest-evidence
    rule; ``lifetime_end`` open or extended past ``last_bar``), so the staging
    gate's P3 probe is green by construction for every priced symbol that has
    an input here.
    """
    seen_ids: set[str] = set()
    out: list[SpineSecurity] = []
    for inp in inputs:
        ticker = inp.ticker.strip().upper()
        if not ticker:
            logger.warning("staging_spine.empty_ticker_skipped", cik=inp.cik)
            continue
        lifetime_start = resolve_lifetime_start(
            fpfd=inp.fpfd,
            first_bar=inp.first_bar,
            fmp_earliest=inp.fmp_earliest,
            now=now,
        )
        lifetime_end = resolve_lifetime_end(
            sec_delisting_date=inp.sec_delisting_date,
            fmp_delisting_date=inp.fmp_delisting_date,
            known_delisting_date=inp.known_delisting_date,
            last_bar=inp.last_bar,
            lifetime_start=lifetime_start,
            still_trading=inp.still_trading,
            ticker=ticker,
        )
        new_id = mint_era_id(inp=inp, now=now, seen_ids=seen_ids)
        source = "sec" if inp.discovery_source == "S" else "fmp"
        metadata_source = (
            "sec_submissions" if inp.asset_class_verified else "alpaca_name"
        )
        out.append(
            SpineSecurity(
                id=new_id,
                ticker=ticker,
                current_ticker=ticker,
                asset_class=(inp.asset_class or "stock").strip().lower(),
                source=source,
                cik=inp.cik,
                legal_name=inp.legal_name,
                lifetime_start=lifetime_start,
                lifetime_end=lifetime_end,
                discovery_source=inp.discovery_source,
                metadata_source=metadata_source,
                asset_class_verified=inp.asset_class_verified,
                first_public_filing_date=inp.fpfd,
            )
        )

    logger.info(
        "staging_spine.assembled",
        n_total=len(out),
        n_sec=sum(1 for s in out if s.source == "sec"),
        n_fmp=sum(1 for s in out if s.source == "fmp"),
        n_delisted=sum(1 for s in out if s.lifetime_end is not None),
        n_verified_asset_class=sum(1 for s in out if s.asset_class_verified),
    )
    return out


__all__ = [
    "PriceBarSpan",
    "SpineBuildInput",
    "SpineSecurity",
    "assemble_spine",
    "mint_era_id",
    "resolve_lifetime_end",
    "resolve_lifetime_start",
]
