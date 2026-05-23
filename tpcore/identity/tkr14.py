"""TKR-14 smart-key generator + validator + decoder.

Per v2.2 spec §1.2-1.5. A 14-char fixed-width identifier encoding:

| Pos | Width | Field |
|-----|-------|-------|
| 1-2 | 2 | Country of incorporation (ISO 3166-1 alpha-2) |
| 3 | 1 | Asset class (S/P/E/F/R/T/A/U/W/N) |
| 4 | 1 | Listing venue at IPO (N/Q/A/B/O/X/Z) — snapshot |
| 5-6 | 2 | Discovery year YY |
| 7 | 1 | Discovery source (F/S/A/O) |
| 8-12 | 5 | Issuer hash — Crockford base32 of SHA-1(country|CIK)[0:25 bits] |
| 13-14 | 2 | ISO 7064 Mod-97-10 check digits |

Charset for issuer-hash excludes I/L/O/U (Crockford base32 — avoids
visual confusion between 1/I/L and 0/O).

ISO 7064 Mod-97-10 check digit is the same algorithm used by LEI
(ISO 17442) — catches all single-digit errors + all adjacent
transposition errors; ~98% typo detection.

Pure functions; no I/O; no DB. Used by `parent_resolver` to mint IDs
on UNKNOWN_TICKER_OBSERVED events.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

# Crockford base32 alphabet: 0-9 A-Z minus I L O U.
# 32 symbols total: 10 digits + 22 letters.
CROCKFORD_BASE32_ALPHABET: str = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# The full regex per spec §1.2 — used by the Postgres CHECK constraint and validate().
TKR14_REGEX: str = (
    r"^[A-Z]{2}"                    # pos 1-2: country
    r"[SPEFRTAUWN]"                 # pos 3: asset class
    r"[NQABOXZ]"                    # pos 4: IPO venue
    r"[0-9]{2}"                     # pos 5-6: discovery year YY
    r"[FSAO]"                       # pos 7: discovery source
    r"[0-9A-HJ-KM-NP-TV-Z]{5}"      # pos 8-12: issuer hash (Crockford base32)
    r"[0-9]{2}"                     # pos 13-14: check digits
    r"$"
)
_TKR14_REGEX_COMPILED: re.Pattern[str] = re.compile(TKR14_REGEX)


class AssetClass(StrEnum):
    """Asset-class char at position 3."""

    STOCK = "S"
    PREFERRED = "P"
    ETF = "E"
    FUND = "F"
    REIT = "R"
    TRUST = "T"
    ADR = "A"
    SPAC_UNIT = "U"
    WARRANT = "W"
    NOTE = "N"


class IPOVenue(StrEnum):
    """Listing-venue-at-IPO char at position 4 (snapshot semantic)."""

    NYSE = "N"
    NASDAQ = "Q"
    AMEX = "A"
    CBOE_BZX = "B"
    OTC = "O"
    FOREIGN_PRIMARY = "X"
    OTHER = "Z"


class DiscoverySource(StrEnum):
    """Discovery-source char at position 7 (provenance snapshot)."""

    FMP = "F"
    SEC = "S"
    ALPACA = "A"
    OTHER = "O"


@dataclass(frozen=True)
class TKR14Segments:
    """Decoded segments of a TKR-14 ID."""

    country: str
    asset_class: AssetClass
    ipo_venue: IPOVenue
    discovery_year_yy: str
    discovery_source: DiscoverySource
    issuer_hash: str
    check: str


def _normalize_legal_name(name: str) -> str:
    """Normalize a legal name to a stable form for hashing.

    Strips accents, uppercases, removes punctuation + extra whitespace.
    Same name strings produce the same hash; small typos do not (by design).
    """
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    upper = ascii_only.upper()
    # Strip punctuation; collapse whitespace
    no_punct = re.sub(r"[^A-Z0-9 ]", " ", upper)
    collapsed = re.sub(r"\s+", " ", no_punct).strip()
    return collapsed


def crockford_base32(value: int, width: int) -> str:
    """Encode a non-negative integer as Crockford base32 (no I/L/O/U).

    Zero-padded to ``width`` chars. Raises ValueError if the value doesn't
    fit in ``width`` chars or is negative.
    """
    if value < 0:
        raise ValueError(f"crockford_base32: value must be non-negative, got {value}")
    if width < 1:
        raise ValueError(f"crockford_base32: width must be >= 1, got {width}")
    max_value = 32**width - 1
    if value > max_value:
        raise ValueError(
            f"crockford_base32: value {value} exceeds max for width={width} (max={max_value})"
        )
    chars: list[str] = []
    n = value
    for _ in range(width):
        chars.append(CROCKFORD_BASE32_ALPHABET[n % 32])
        n //= 32
    return "".join(reversed(chars))


def iso_7064_mod_97_10(prefix: str) -> str:
    """Compute the ISO 7064 Mod-97-10 check digits (2 chars) for a prefix.

    Algorithm (per ISO/IEC 7064, used by LEI ISO 17442):
      1. Remap each char: A-Z → 10-35; 0-9 → themselves.
      2. Append '00' to the remapped sequence.
      3. Convert to one big decimal integer N.
      4. check = 98 - (N mod 97), zero-padded to 2 digits.

    Validation (separate concern): the full string (prefix+check), remapped
    and treated as an integer, must satisfy ``N mod 97 == 1``.

    Raises ValueError if the prefix contains chars outside [0-9A-Z].
    """
    if not re.fullmatch(r"[0-9A-Z]+", prefix):
        raise ValueError(
            f"iso_7064_mod_97_10: prefix must be uppercase alphanumeric only, got {prefix!r}"
        )
    digits: list[str] = []
    for ch in prefix:
        if ch.isdigit():
            digits.append(ch)
        else:
            digits.append(str(ord(ch) - ord("A") + 10))
    n = int("".join(digits) + "00")
    check_int = 98 - (n % 97)
    return f"{check_int:02d}"


def _verify_iso_7064(full_id: str) -> bool:
    """Verify a full TKR-14 string satisfies the ISO 7064 Mod-97-10 invariant.

    Returns True if ``N mod 97 == 1`` for the remapped integer.
    """
    digits: list[str] = []
    for ch in full_id:
        if ch.isdigit():
            digits.append(ch)
        else:
            digits.append(str(ord(ch) - ord("A") + 10))
    return int("".join(digits)) % 97 == 1


def _issuer_hash(country: str, cik: str | None, legal_name: str, salt: int = 0) -> str:
    """Compute the 5-char Crockford base32 issuer hash.

    SHA-1 over ``country|CIK`` (preferred) or ``country|normalized_legal_name``
    (fallback); take the top 25 bits; render as 5 Crockford base32 chars.

    ``salt``: when > 0, appended to the seed as ``|salt={n}`` to break
    collisions. Used by parent_resolver / backfill stages that detect a
    UNIQUE violation on insert and retry with an incrementing salt.
    Birthday-paradox at 13K rows is ~1.7%; salt=1 typically resolves it
    on the first retry. Salt=0 (default) produces the canonical no-collision
    hash for fresh mints.
    """
    if cik:
        seed = f"{country}|{cik}"
    else:
        seed = f"{country}|{_normalize_legal_name(legal_name)}"
    if salt > 0:
        seed = f"{seed}|salt={salt}"
    digest = hashlib.sha1(seed.encode("utf-8")).digest()
    # Top 25 bits of the 160-bit SHA-1 digest
    top_32_bits = int.from_bytes(digest[:4], "big")
    top_25_bits = top_32_bits >> 7
    return crockford_base32(top_25_bits, width=5)


def mint(
    *,
    country: str,
    asset_class: AssetClass | str,
    ipo_venue: IPOVenue | str,
    discovery_source: DiscoverySource | str,
    cik: str | None,
    legal_name: str,
    now: datetime,
    salt: int = 0,
) -> str:
    """Mint a new TKR-14 identifier from immutable / at-mint-snapshot facts.

    All arguments are keyword-only to prevent positional confusion. The
    resulting 14-char string is deterministic given the same inputs (same
    country+CIK or country+legal_name produces the same issuer hash; same
    year+source+venue+asset_class produces the same prefix).

    Args:
      country: ISO 3166-1 alpha-2 code (e.g. "US", "JP", "GB"). Must be 2 uppercase letters.
      asset_class: AssetClass enum or single-letter code (S/P/E/F/R/T/A/U/W/N).
      ipo_venue: IPOVenue enum or single-letter code (N/Q/A/B/O/X/Z).
      discovery_source: DiscoverySource enum or single-letter code (F/S/A/O).
      cik: SEC CIK string (preferred for issuer-hash seed). None for non-SEC issuers.
      legal_name: Issuer legal name (fallback seed when CIK is None).
      now: UTC datetime; year-mod-100 becomes the discovery_year_yy segment.
      salt: collision-retry counter (default 0). Callers that catch a UNIQUE
        violation on insert should retry with salt=1, salt=2, ... until the
        generated id no longer collides. Birthday-paradox math at 13K rows
        + 25-bit hash = ~1.7% probability of ANY collision; salt=1 typically
        resolves on first retry (each salt increment changes ALL bits of
        the SHA-1 digest). Salt=0 produces the canonical no-collision hash.

    Returns:
      14-char TKR-14 string matching TKR14_REGEX.

    Raises:
      ValueError: if country is not 2-uppercase-letter; if enum coercion fails;
                  if both cik and legal_name are empty; if salt is negative.
    """
    if not (isinstance(country, str) and len(country) == 2 and country.isalpha() and country.isupper()):
        raise ValueError(f"mint: country must be 2 uppercase letters (ISO 3166-1 alpha-2), got {country!r}")
    if not cik and not legal_name:
        raise ValueError("mint: at least one of (cik, legal_name) must be non-empty")
    if salt < 0:
        raise ValueError(f"mint: salt must be >= 0, got {salt}")

    ac = asset_class.value if isinstance(asset_class, AssetClass) else AssetClass(asset_class).value
    venue = ipo_venue.value if isinstance(ipo_venue, IPOVenue) else IPOVenue(ipo_venue).value
    src = discovery_source.value if isinstance(discovery_source, DiscoverySource) else DiscoverySource(discovery_source).value

    yy = f"{now.year % 100:02d}"
    issuer = _issuer_hash(country=country, cik=cik, legal_name=legal_name, salt=salt)
    prefix = f"{country}{ac}{venue}{yy}{src}{issuer}"
    assert len(prefix) == 12, f"prefix length mismatch: got {len(prefix)}, expected 12"
    check = iso_7064_mod_97_10(prefix)
    return f"{prefix}{check}"


def validate(tkr14_id: str) -> bool:
    """Return True if the string is a structurally + check-digit valid TKR-14."""
    if not isinstance(tkr14_id, str) or len(tkr14_id) != 14:
        return False
    if not _TKR14_REGEX_COMPILED.match(tkr14_id):
        return False
    return _verify_iso_7064(tkr14_id)


def decode(tkr14_id: str) -> TKR14Segments:
    """Decode a TKR-14 string into its segments.

    Raises ValueError if the input is structurally invalid OR fails the check digit.
    """
    if not validate(tkr14_id):
        raise ValueError(f"decode: invalid TKR-14 (regex or check-digit failure): {tkr14_id!r}")
    return TKR14Segments(
        country=tkr14_id[0:2],
        asset_class=AssetClass(tkr14_id[2]),
        ipo_venue=IPOVenue(tkr14_id[3]),
        discovery_year_yy=tkr14_id[4:6],
        discovery_source=DiscoverySource(tkr14_id[6]),
        issuer_hash=tkr14_id[7:12],
        check=tkr14_id[12:14],
    )
