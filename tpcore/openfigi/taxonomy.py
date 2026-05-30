"""OpenFIGI security-type → STE asset_class / instrument_subtype mapping.

2026-05-30 quantitative finance expert review (spec
docs/superpowers/specs/2026-05-30-asset-class-refinement.md):

OpenFIGI's ``securityType2`` is the most specific instrument
classification their API exposes (Bloomberg-derived). We map it to a
9-class internal taxonomy + an optional ``instrument_subtype`` for
finer-grained distinctions:

    OpenFIGI securityType2              STE asset_class  subtype
    ─────────────────────────────────── ───────────────  ─────────
    Common Stock                        stock            None
    Common Stock (REIT-like names)      reit             None
    ADR / GDR                           adr              sponsored
    Preferred Stock                     preferred        None
    Preference / Preference Share       preferred        None
    ETP (vanilla)                       etf              vanilla
    ETP (leveraged / inverse)           etf              leveraged or inverse
    ETN                                 etn              None
    Closed-End Fund                     cef              None
    Mutual Fund / Open-End Fund         fund             None
    SPAC Class A (no suffix)            spac             share
    SPAC Unit (.U suffix)               spac             unit
    SPAC Warrant (.W / .WS suffix)      spac             warrant

REIT detection: OpenFIGI labels REITs as ``Common Stock`` in
securityType2 but the ``name`` field typically contains "REIT" or
"Real Estate Investment Trust". We do a secondary name-pattern check.

SPAC subtype detection: OpenFIGI's ``securityType2`` for SPACs is
sometimes ``Common Stock`` (for the Class A share). The ticker suffix
+ name pattern is more reliable. We use ticker suffix as the primary
discriminator (.U → unit, .W/.WS → warrant) and fall back to name
patterns ("Unit", "Warrant", "Right").

Unknowns: when OpenFIGI returns a ``securityType2`` we don't have a
mapping for, fall back to the operator's existing FMP-derived
``asset_class`` value (don't break what's already classified). Log a
warning so we can extend the map.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)


# Valid asset_class values — MUST match the platform CHECK constraint
# at platform/migrations/versions/20260530_0100_asset_class_refinement.py.
VALID_ASSET_CLASSES: frozenset[str] = frozenset({
    "stock", "adr", "preferred", "reit",
    "etf", "etn", "cef", "fund",
    "spac",
})


# Valid instrument_subtype values — MUST match the CHECK constraint
# in the same migration.
VALID_INSTRUMENT_SUBTYPES: frozenset[str] = frozenset({
    "share", "unit", "warrant",
    "vanilla", "leveraged", "inverse",
    "sponsored", "unsponsored",
})


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """Output of taxonomy mapping for a single ticker."""

    asset_class: str
    instrument_subtype: str | None
    confidence: float
    """0.0..1.0 — 1.0 means OpenFIGI gave us a perfect direct mapping,
    < 1.0 means we used heuristics (name pattern, ticker suffix)."""
    source: str
    """One of: 'openfigi_direct', 'openfigi_heuristic', 'ticker_suffix',
    'fallback'. Logged for auditability."""


# ───────────────────────── primary table ─────────────────────────
# Direct securityType2 → asset_class mappings. Confidence 1.0 when
# there's no ambiguity.
_SECURITYTYPE2_DIRECT: dict[str, tuple[str, str | None]] = {
    # Common equity
    "common stock": ("stock", None),

    # REITs (Bloomberg labels these directly)
    "reit": ("reit", None),
    "real estate investment trust": ("reit", None),

    # Partnerships / MLPs — treat as stock for screening purposes;
    # tax treatment differs but daily-price monitoring is the same.
    "mlp": ("stock", None),
    "partnership shares": ("stock", None),
    "limited partnership": ("stock", None),

    # Depositary receipts
    "adr": ("adr", "sponsored"),
    "gdr": ("adr", "sponsored"),  # global DR — treat same as ADR
    "depositary receipt": ("adr", "sponsored"),

    # Preferred
    "preferred": ("preferred", None),
    "preference": ("preferred", None),
    "preference share": ("preferred", None),
    "preferred stock": ("preferred", None),

    # Funds
    "closed-end fund": ("cef", None),
    "open-end fund": ("fund", None),
    "mutual fund": ("fund", None),
    "investment fund": ("fund", None),
    "money market fund": ("fund", None),

    # Exchange-traded
    "etp": ("etf", "vanilla"),
    "etf": ("etf", "vanilla"),
    "exchange-traded fund": ("etf", "vanilla"),
    "exchange-traded note": ("etn", None),
    "etn": ("etn", None),

    # SPACs — direct Bloomberg labels (rare)
    "spac": ("spac", "share"),
    "special purpose acquisition company": ("spac", "share"),

    # Warrants (Bloomberg returns these as their own category)
    "warrant": ("spac", "warrant"),
    "equity wrt": ("spac", "warrant"),
    "warrants": ("spac", "warrant"),

    # Rights — treat as warrant-class (similar mechanics).
    "right": ("spac", "warrant"),
    "rights": ("spac", "warrant"),
    "purchase right": ("spac", "warrant"),

    # Units
    "unit": ("spac", "unit"),
    "units": ("spac", "unit"),
}


# ───────────────────────── name patterns ─────────────────────────


_REIT_NAME_PATTERN = re.compile(
    r"\b(REIT|Real Estate Investment Trust)\b", re.IGNORECASE,
)

# SPAC name pattern — used to detect SPACs that OpenFIGI labels as
# "Common Stock". Includes "Acquisition Corp", "Acquisition Holdings",
# generic "SPAC" mentions.
_SPAC_NAME_PATTERN = re.compile(
    r"\b("
    r"Acquisition\s+(?:Corp|Holdings|Corporation|Company|Co)"
    r"|SPAC"
    r"|Capital\s+Acquisition"
    r")\b",
    re.IGNORECASE,
)

# SPAC unit name pattern (the "Class A share + fractional warrant" basket).
_UNIT_NAME_PATTERN = re.compile(
    r"\b(?:Unit|Units)\b", re.IGNORECASE,
)

# SPAC warrant name pattern. Includes Bloomberg-style abbreviations
# (Wt/Wts) and the common -WARR suffix variant. Word-boundary check
# guards against the literal stem "warranty" / "warranties".
_WARRANT_NAME_PATTERN = re.compile(
    r"(?:\b(?:Warrant|Warrants|Wt|Wts)\b|-WARR\b)", re.IGNORECASE,
)

# CEF name pattern fallback when securityType2 is generic.
_CEF_NAME_PATTERN = re.compile(
    r"\b(?:Closed-End|Closed End)\s+Fund\b", re.IGNORECASE,
)

# Leveraged / inverse ETF name patterns (Direxion, ProShares,
# MicroSectors typical naming).
_LEVERAGED_ETF_PATTERN = re.compile(
    r"\b(?:Ultra|2X|3X|Bull|UltraPro)\b", re.IGNORECASE,
)
_INVERSE_ETF_PATTERN = re.compile(
    r"\b(?:Inverse|Bear|Short|-1X|-2X|-3X)\b", re.IGNORECASE,
)


# ───────────────────────── ticker-suffix rules ─────────────────────


# Tickers ending in these strings strongly indicate SPAC components.
_SPAC_UNIT_SUFFIXES = ("U", "UN")  # AACIU = unit; PCCTU = unit
_SPAC_WARRANT_SUFFIXES = ("W", "WS", "WW")  # ABCDW = warrant; XYZWW = double warrant

# ADR suffix conventions (NYSE/NASDAQ).
_ADR_SUFFIXES = ("Y", "F")  # .Y typically ADR, .F typically FPI


def classify(
    *,
    ticker: str,
    security_type: str | None,
    security_type2: str | None,
    name: str | None,
    fallback_asset_class: str | None = None,
) -> ClassificationResult:
    """Map an OpenFIGI result to (asset_class, instrument_subtype).

    Always returns a result — falls back to ``fallback_asset_class``
    (the existing FMP-derived value) when OpenFIGI doesn't give us
    enough signal. Never returns an invalid asset_class.
    """
    securitytype2_norm = (security_type2 or "").strip().lower()
    securitytype_norm = (security_type or "").strip().lower()
    name_str = (name or "").strip()
    ticker_str = (ticker or "").strip().upper()

    # ─── 0. Authoritative subtype labels from OpenFIGI win over
    # ticker-suffix heuristics. When Bloomberg explicitly returns
    # "Warrant" / "Unit" / "Right", that's dispositive — don't try
    # to outsmart it with name-pattern guessing. ───
    if securitytype2_norm in ("warrant", "warrants", "equity wrt",
                              "right", "rights", "purchase right"):
        return ClassificationResult(
            asset_class="spac", instrument_subtype="warrant",
            confidence=1.0, source="openfigi_direct",
        )
    if securitytype2_norm in ("unit", "units"):
        return ClassificationResult(
            asset_class="spac", instrument_subtype="unit",
            confidence=1.0, source="openfigi_direct",
        )

    # ─── 1. SPAC subtype heuristics (ticker suffix + name pattern)
    # take next precedence — OpenFIGI often labels SPAC Class A shares
    # as plain "Common Stock", and warrants/units sometimes lack the
    # securityType2 label. ───
    if _is_spac_warrant(ticker_str, name_str):
        return ClassificationResult(
            asset_class="spac",
            instrument_subtype="warrant",
            confidence=0.95,
            source="ticker_suffix",
        )
    if _is_spac_unit(ticker_str, name_str):
        return ClassificationResult(
            asset_class="spac",
            instrument_subtype="unit",
            confidence=0.95,
            source="ticker_suffix",
        )
    if _is_spac_share(ticker_str, name_str, securitytype2_norm):
        return ClassificationResult(
            asset_class="spac",
            instrument_subtype="share",
            confidence=0.85,
            source="openfigi_heuristic",
        )

    # ─── 2. REIT detection — OpenFIGI labels these as "Common Stock"
    # so we have to read the name ───
    if securitytype2_norm == "common stock" and _REIT_NAME_PATTERN.search(name_str):
        return ClassificationResult(
            asset_class="reit",
            instrument_subtype=None,
            confidence=0.85,
            source="openfigi_heuristic",
        )

    # ─── 3. CEF detection — sometimes labeled "Common Stock" with
    # "Closed-End Fund" in the name ───
    if (
        securitytype2_norm in ("common stock", "fund", "")
        and _CEF_NAME_PATTERN.search(name_str)
    ):
        return ClassificationResult(
            asset_class="cef",
            instrument_subtype=None,
            confidence=0.85,
            source="openfigi_heuristic",
        )

    # ─── 4. ETF subtype refinement + Open-End Fund disambiguation ───
    direct = _SECURITYTYPE2_DIRECT.get(securitytype2_norm)
    if direct:
        ac, subtype = direct
        # ETF / Mutual Fund disambiguation: Bloomberg labels exchange-
        # listed ETFs as "Open-End Fund" (technically true: most ETFs
        # are structured as open-end investment companies under the
        # '40 Act). When the existing operator-supplied classification
        # is 'etf', honour it — the FMP /profile data we already have
        # said "ETF" for a reason (exchange listing + creation/redemption
        # mechanism). For tickers whose existing class was NOT 'etf',
        # treat "Open-End Fund" as a true mutual fund.
        if ac == "fund" and fallback_asset_class == "etf":
            ac = "etf"
            subtype = "vanilla"
            if _LEVERAGED_ETF_PATTERN.search(name_str):
                subtype = "leveraged"
            elif _INVERSE_ETF_PATTERN.search(name_str):
                subtype = "inverse"
        elif ac == "etf":
            if _LEVERAGED_ETF_PATTERN.search(name_str):
                subtype = "leveraged"
            elif _INVERSE_ETF_PATTERN.search(name_str):
                subtype = "inverse"
            else:
                subtype = "vanilla"
        return ClassificationResult(
            asset_class=ac,
            instrument_subtype=subtype,
            confidence=1.0,
            source="openfigi_direct",
        )

    # ─── 5. securityType fallback (the less-specific OpenFIGI field) ─
    direct = _SECURITYTYPE2_DIRECT.get(securitytype_norm)
    if direct:
        ac, subtype = direct
        return ClassificationResult(
            asset_class=ac,
            instrument_subtype=subtype,
            confidence=0.7,
            source="openfigi_direct",
        )

    # ─── 6. Final fallback to operator-supplied existing value ───
    if fallback_asset_class and fallback_asset_class in VALID_ASSET_CLASSES:
        logger.warning(
            "openfigi.taxonomy.fallback_to_existing",
            ticker=ticker_str,
            security_type=security_type,
            security_type2=security_type2,
            name=name_str,
            fallback=fallback_asset_class,
        )
        return ClassificationResult(
            asset_class=fallback_asset_class,
            instrument_subtype=None,
            confidence=0.3,
            source="fallback",
        )

    # ─── 7. Last resort — assume stock so the DB CHECK still passes ─
    logger.warning(
        "openfigi.taxonomy.no_classification",
        ticker=ticker_str,
        security_type=security_type,
        security_type2=security_type2,
        name=name_str,
    )
    return ClassificationResult(
        asset_class="stock",
        instrument_subtype=None,
        confidence=0.1,
        source="fallback",
    )


def _is_spac_warrant(ticker: str, name: str) -> bool:
    """Detect SPAC warrants via ticker suffix + name pattern."""
    if name and _WARRANT_NAME_PATTERN.search(name):
        return True
    # Ticker suffix — but only when the BASE prefix isn't a known
    # non-SPAC ticker (heuristic — most SPAC warrants have an "obvious
    # parent" ticker name with the same root + .W).
    for suf in _SPAC_WARRANT_SUFFIXES:
        if ticker.endswith(suf) and len(ticker) >= 4:
            # Avoid false positives: classic warrant suffixes are
            # appended to a base ticker that's 3-5 chars. Tickers like
            # "W" alone or 2-char prefix are out of the SPAC norm.
            if len(ticker) - len(suf) >= 3:
                # Also require a name hint OR no name (be lenient).
                # Common SPAC warrant names contain "Warrant" or "Wt".
                if not name or _is_likely_spac_warrant_name(name):
                    return True
    return False


def _is_likely_spac_warrant_name(name: str) -> bool:
    """A name pattern that's typical of a SPAC warrant."""
    lower = name.lower()
    # Warrant-specific terms, including the Bloomberg-style abbreviations.
    return any(p in lower for p in (
        "warrant", "warrants", " wt ", " wts ", "wt-", "-wt",
        "-warr", "purch right",
    ))


def _is_spac_unit(ticker: str, name: str) -> bool:
    """Detect SPAC units via ticker suffix + name pattern."""
    if name and _UNIT_NAME_PATTERN.search(name):
        return True
    for suf in _SPAC_UNIT_SUFFIXES:
        if ticker.endswith(suf) and len(ticker) >= 4:
            if len(ticker) - len(suf) >= 3:
                return True
    return False


def _is_spac_share(ticker: str, name: str, securitytype2: str) -> bool:
    """Detect SPAC Class A shares — only triggered if not already
    matched as warrant/unit above. Looks for SPAC name patterns."""
    if _SPAC_NAME_PATTERN.search(name or ""):
        return True
    return False
