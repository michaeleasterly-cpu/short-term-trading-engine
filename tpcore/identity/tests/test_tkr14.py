"""TKR-14 smart-key — structural + algorithmic correctness tests."""
from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from tpcore.identity.tkr14 import (
    CROCKFORD_BASE32_ALPHABET,
    TKR14_REGEX,
    AssetClass,
    DiscoverySource,
    IPOVenue,
    crockford_base32,
    decode,
    iso_7064_mod_97_10,
    mint,
    validate,
)

# ─────────────────────────────────────────────────────────────────
# Crockford base32 — alphabet + encoding
# ─────────────────────────────────────────────────────────────────


def test_crockford_alphabet_has_32_symbols_no_iluo():
    """Crockford base32 alphabet must have 32 unique chars; exclude I, L, O, U."""
    assert len(CROCKFORD_BASE32_ALPHABET) == 32
    assert len(set(CROCKFORD_BASE32_ALPHABET)) == 32
    for forbidden in "ILOU":
        assert forbidden not in CROCKFORD_BASE32_ALPHABET, f"{forbidden!r} must be excluded"


def test_crockford_base32_zero():
    assert crockford_base32(0, width=5) == "00000"


def test_crockford_base32_max_value():
    """32^5 - 1 = 33,554,431 → all-Z (last alphabet symbol)."""
    max_value = 32**5 - 1
    encoded = crockford_base32(max_value, width=5)
    assert encoded == CROCKFORD_BASE32_ALPHABET[-1] * 5


def test_crockford_base32_rejects_negative():
    with pytest.raises(ValueError, match="non-negative"):
        crockford_base32(-1, width=5)


def test_crockford_base32_rejects_overflow():
    with pytest.raises(ValueError, match="exceeds max"):
        crockford_base32(32**5, width=5)  # one over max


def test_crockford_base32_rejects_zero_width():
    with pytest.raises(ValueError, match="width must be"):
        crockford_base32(0, width=0)


# ─────────────────────────────────────────────────────────────────
# ISO 7064 Mod-97-10 check digits
# ─────────────────────────────────────────────────────────────────


def test_iso_7064_check_digits_are_two_chars():
    """Check output is always 2 digits, zero-padded."""
    for prefix in ("AAAAAAAAAAAA", "USSN26F7K3X9", "ZZZZZZZZZZZZ", "000000000000"):
        check = iso_7064_mod_97_10(prefix)
        assert re.fullmatch(r"[0-9]{2}", check), f"{prefix} → {check} not 2 digits"


def test_iso_7064_full_string_validates_mod_97_eq_1():
    """The full string (prefix+check), interpreted as integer with A-Z→10-35, mod 97 = 1."""
    prefix = "USSN26F7K3X9"
    check = iso_7064_mod_97_10(prefix)
    full = prefix + check
    digits = "".join(c if c.isdigit() else str(ord(c) - ord("A") + 10) for c in full)
    assert int(digits) % 97 == 1


def test_iso_7064_rejects_lowercase():
    with pytest.raises(ValueError, match="uppercase alphanumeric"):
        iso_7064_mod_97_10("ussn26f7k3x9")


def test_iso_7064_rejects_punctuation():
    with pytest.raises(ValueError, match="uppercase alphanumeric"):
        iso_7064_mod_97_10("USSN-26F7K3X9")


# ─────────────────────────────────────────────────────────────────
# mint() — structural + determinism
# ─────────────────────────────────────────────────────────────────


_NOW_2026 = datetime(2026, 5, 23, tzinfo=UTC)


def test_mint_produces_14_chars_matching_regex():
    tkr = mint(
        country="US",
        asset_class=AssetClass.STOCK,
        ipo_venue=IPOVenue.NASDAQ,
        discovery_source=DiscoverySource.FMP,
        cik="0000320193",
        legal_name="APPLE INC",
        now=_NOW_2026,
    )
    assert len(tkr) == 14
    assert re.fullmatch(TKR14_REGEX, tkr), f"{tkr} fails TKR14_REGEX"


def test_mint_country_segment_is_first_two_chars():
    for country in ("US", "JP", "GB", "CA", "AU"):
        tkr = mint(
            country=country, asset_class=AssetClass.STOCK, ipo_venue=IPOVenue.OTHER,
            discovery_source=DiscoverySource.FMP, cik=None, legal_name="ACME CORP",
            now=_NOW_2026,
        )
        assert tkr.startswith(country), f"{tkr} should start with {country}"


def test_mint_asset_class_segment_is_third_char():
    for ac in AssetClass:
        tkr = mint(
            country="US", asset_class=ac, ipo_venue=IPOVenue.OTHER,
            discovery_source=DiscoverySource.FMP, cik="0001234567", legal_name="X",
            now=_NOW_2026,
        )
        assert tkr[2] == ac.value, f"asset_class={ac.value} should be char 3 of {tkr}"


def test_mint_year_segment_is_yy():
    for year, expected in [(2020, "20"), (2026, "26"), (2099, "99"), (2000, "00")]:
        tkr = mint(
            country="US", asset_class=AssetClass.STOCK, ipo_venue=IPOVenue.NYSE,
            discovery_source=DiscoverySource.FMP, cik="0001234567", legal_name="X",
            now=datetime(year, 1, 1, tzinfo=UTC),
        )
        assert tkr[4:6] == expected, f"year {year} → {tkr[4:6]}, expected {expected}"


def test_mint_is_deterministic_for_same_inputs():
    kwargs = dict(
        country="US", asset_class=AssetClass.STOCK, ipo_venue=IPOVenue.NYSE,
        discovery_source=DiscoverySource.FMP, cik="0000320193", legal_name="APPLE INC",
        now=_NOW_2026,
    )
    assert mint(**kwargs) == mint(**kwargs)


def test_mint_differs_when_cik_differs():
    base = dict(
        country="US", asset_class=AssetClass.STOCK, ipo_venue=IPOVenue.NYSE,
        discovery_source=DiscoverySource.FMP, legal_name="DOES NOT MATTER",
        now=_NOW_2026,
    )
    a = mint(**base, cik="0000320193")  # AAPL
    b = mint(**base, cik="0001326801")  # META
    assert a != b


def test_mint_falls_back_to_legal_name_when_cik_is_none():
    """Foreign issuers without SEC CIK still get a stable hash via legal name."""
    a = mint(
        country="JP", asset_class=AssetClass.STOCK, ipo_venue=IPOVenue.FOREIGN_PRIMARY,
        discovery_source=DiscoverySource.FMP, cik=None, legal_name="TOYOTA MOTOR CORP",
        now=_NOW_2026,
    )
    b = mint(
        country="JP", asset_class=AssetClass.STOCK, ipo_venue=IPOVenue.FOREIGN_PRIMARY,
        discovery_source=DiscoverySource.FMP, cik=None, legal_name="TOYOTA MOTOR CORP",
        now=_NOW_2026,
    )
    assert a == b, "Same legal-name fallback must produce same hash"


def test_mint_normalizes_legal_name_variations():
    """Accents, punctuation, extra whitespace should not change the hash."""
    canonical = mint(
        country="DE", asset_class=AssetClass.STOCK, ipo_venue=IPOVenue.FOREIGN_PRIMARY,
        discovery_source=DiscoverySource.FMP, cik=None, legal_name="SOCIETE GENERALE",
        now=_NOW_2026,
    )
    accented = mint(
        country="DE", asset_class=AssetClass.STOCK, ipo_venue=IPOVenue.FOREIGN_PRIMARY,
        discovery_source=DiscoverySource.FMP, cik=None, legal_name="Société Générale",
        now=_NOW_2026,
    )
    extra_spaces = mint(
        country="DE", asset_class=AssetClass.STOCK, ipo_venue=IPOVenue.FOREIGN_PRIMARY,
        discovery_source=DiscoverySource.FMP, cik=None, legal_name="  Societe   Generale  ",
        now=_NOW_2026,
    )
    assert canonical == accented == extra_spaces


def test_mint_rejects_invalid_country():
    bad = ["us", "USA", "U1", "  ", "X"]
    for c in bad:
        with pytest.raises(ValueError, match="ISO 3166-1 alpha-2"):
            mint(
                country=c, asset_class=AssetClass.STOCK, ipo_venue=IPOVenue.NYSE,
                discovery_source=DiscoverySource.FMP, cik="0001234567", legal_name="X",
                now=_NOW_2026,
            )


def test_mint_rejects_empty_cik_and_legal_name():
    with pytest.raises(ValueError, match="at least one of"):
        mint(
            country="US", asset_class=AssetClass.STOCK, ipo_venue=IPOVenue.NYSE,
            discovery_source=DiscoverySource.FMP, cik=None, legal_name="",
            now=_NOW_2026,
        )


# ─────────────────────────────────────────────────────────────────
# validate() — accepts valid, rejects malformed + bad check digit
# ─────────────────────────────────────────────────────────────────


def test_validate_accepts_freshly_minted():
    """Every freshly-minted ID must pass validate."""
    for cik in ("0000320193", "0001326801", "0001067983"):
        tkr = mint(
            country="US", asset_class=AssetClass.STOCK, ipo_venue=IPOVenue.NYSE,
            discovery_source=DiscoverySource.FMP, cik=cik, legal_name="X",
            now=_NOW_2026,
        )
        assert validate(tkr), f"{tkr} (freshly minted) should validate"


def test_validate_rejects_wrong_length():
    assert not validate("US")
    assert not validate("USSN26F7K3X90")     # 13 chars
    assert not validate("USSN26F7K3X9045")   # 15 chars


def test_validate_rejects_lowercase():
    """Postgres CHECK constraint enforces uppercase; same here."""
    tkr = mint(
        country="US", asset_class=AssetClass.STOCK, ipo_venue=IPOVenue.NYSE,
        discovery_source=DiscoverySource.FMP, cik="0000320193", legal_name="X",
        now=_NOW_2026,
    )
    assert not validate(tkr.lower())


def test_validate_rejects_bad_check_digit():
    """A single-char mutation in the check should fail validate."""
    tkr = mint(
        country="US", asset_class=AssetClass.STOCK, ipo_venue=IPOVenue.NYSE,
        discovery_source=DiscoverySource.FMP, cik="0000320193", legal_name="X",
        now=_NOW_2026,
    )
    # Flip last char (a digit) to a different digit
    last = tkr[-1]
    replacement = "0" if last != "0" else "1"
    mutated = tkr[:-1] + replacement
    assert not validate(mutated), f"{mutated} (check-digit-corrupted) should fail"


def test_validate_rejects_invalid_asset_class_segment():
    # Construct a 14-char string with an invalid asset-class char (X is not in S/P/E/F/R/T/A/U/W/N)
    bad = "USXN26F00000QQ"  # X at pos 3 — not a valid asset-class
    assert not validate(bad)


def test_validate_rejects_issuer_hash_with_forbidden_chars():
    """I/L/O/U are excluded from Crockford base32 — must not appear in pos 8-12."""
    for forbidden in "ILOU":
        # Replace the first issuer-hash char with a forbidden letter
        bad = "USSN26F" + forbidden + "0000" + "00"
        assert not validate(bad), f"{bad} contains forbidden Crockford char {forbidden}"


# ─────────────────────────────────────────────────────────────────
# decode() — structured access to segments
# ─────────────────────────────────────────────────────────────────


def test_decode_recovers_all_segments():
    tkr = mint(
        country="US",
        asset_class=AssetClass.ETF,
        ipo_venue=IPOVenue.NASDAQ,
        discovery_source=DiscoverySource.FMP,
        cik=None,
        legal_name="INVESCO QQQ TRUST",
        now=datetime(2020, 5, 1, tzinfo=UTC),
    )
    segs = decode(tkr)
    assert segs.country == "US"
    assert segs.asset_class == AssetClass.ETF
    assert segs.ipo_venue == IPOVenue.NASDAQ
    assert segs.discovery_year_yy == "20"
    assert segs.discovery_source == DiscoverySource.FMP
    assert len(segs.issuer_hash) == 5
    assert len(segs.check) == 2


def test_decode_rejects_invalid():
    with pytest.raises(ValueError, match="invalid TKR-14"):
        decode("not_a_tkr14_id")
    with pytest.raises(ValueError, match="invalid TKR-14"):
        decode("USSN26F7K3X9XX")  # check digit "XX" is not digits


# ─────────────────────────────────────────────────────────────────
# Round-trip — mint → validate → decode for many random-ish inputs
# ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("seed_n", range(100))
def test_mint_validate_decode_round_trip(seed_n):
    """100 deterministic mints all round-trip through validate + decode without error."""
    # Vary inputs across the parameter space
    countries = ["US", "JP", "GB", "CA", "DE", "HK", "AU", "KY", "BM"]
    asset_classes = list(AssetClass)
    ipo_venues = list(IPOVenue)
    sources = list(DiscoverySource)

    tkr = mint(
        country=countries[seed_n % len(countries)],
        asset_class=asset_classes[seed_n % len(asset_classes)],
        ipo_venue=ipo_venues[seed_n % len(ipo_venues)],
        discovery_source=sources[seed_n % len(sources)],
        cik=f"{seed_n:010d}",
        legal_name=f"TEST ISSUER {seed_n}",
        now=datetime(2020 + (seed_n % 10), 1, 1, tzinfo=UTC),
    )
    assert validate(tkr), f"round-trip {seed_n}: {tkr} failed validate"
    segs = decode(tkr)
    assert segs.country == countries[seed_n % len(countries)]
    assert segs.discovery_year_yy == f"{(2020 + (seed_n % 10)) % 100:02d}"
