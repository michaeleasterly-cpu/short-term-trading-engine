"""TKR-14 salt-retry parameter — collision-recovery via salt incrementation."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tpcore.identity.tkr14 import (
    AssetClass,
    DiscoverySource,
    IPOVenue,
    mint,
    validate,
)

_NOW = datetime(2026, 5, 23, tzinfo=UTC)


def _kwargs() -> dict[str, object]:
    return {
        "country": "US",
        "asset_class": AssetClass.STOCK,
        "ipo_venue": IPOVenue.NYSE,
        "discovery_source": DiscoverySource.FMP,
        "cik": "0000320193",
        "legal_name": "APPLE INC",
        "now": _NOW,
    }


def test_salt_zero_is_default_and_matches_no_salt():
    """salt=0 (default) produces the canonical hash."""
    a = mint(**_kwargs())
    b = mint(**_kwargs(), salt=0)
    assert a == b


def test_salt_nonzero_changes_the_id():
    """salt=1 must change the issuer-hash (and therefore the full id)."""
    a = mint(**_kwargs(), salt=0)
    b = mint(**_kwargs(), salt=1)
    assert a != b


def test_salt_each_increment_produces_distinct_id():
    """salts 0..10 all produce distinct ids (SHA-1 avalanche)."""
    ids = {mint(**_kwargs(), salt=s) for s in range(11)}
    assert len(ids) == 11


def test_salt_id_remains_valid_per_regex_and_check_digit():
    """Salted ids still pass full validate() (regex + ISO 7064)."""
    for s in range(5):
        tkr = mint(**_kwargs(), salt=s)
        assert validate(tkr), f"salt={s} produced invalid id {tkr}"


def test_salt_changes_only_issuer_hash_segment():
    """Pos 1-7 (country/AC/venue/YY/source) + pos 13-14 (check) may change;
    Pos 8-12 (issuer hash) definitely changes between salts."""
    a = mint(**_kwargs(), salt=0)
    b = mint(**_kwargs(), salt=1)
    # Prefix pos 1-7 derived from non-hash inputs — should be identical.
    assert a[:7] == b[:7], f"prefix segment 1-7 should be stable, got {a[:7]} vs {b[:7]}"
    # Issuer hash (pos 8-12) must differ.
    assert a[7:12] != b[7:12], "issuer-hash segment must change between salts"


def test_salt_rejects_negative():
    """Negative salt raises ValueError."""
    with pytest.raises(ValueError, match="salt must be >= 0"):
        mint(**_kwargs(), salt=-1)


def test_salt_deterministic_for_same_value():
    """salt=N produces the same id every call (just like salt=0)."""
    assert mint(**_kwargs(), salt=42) == mint(**_kwargs(), salt=42)


def test_collision_recovery_pattern():
    """Simulates a caller's salt-retry loop: find a salt that avoids
    a specific colliding id (from the operator-observed P5 backfill).

    The two real collisions surfaced in the live P5 backfill were:
      USEZ26OB884316: FLYT + SAMM
      USUZ26OQ5WGG88: ETWOW + SPEGU

    A caller that gets a UNIQUE-violation on insert should iterate salt
    1, 2, 3, ... up to a small max (say 10) and use the first salt whose
    output isn't already in the parent table.
    """
    # Pretend the canonical mint produced a colliding id we want to avoid.
    canonical = mint(**_kwargs(), salt=0)
    seen = {canonical}
    # Iterate salts until we find one not in seen.
    chosen = None
    for s in range(1, 11):
        candidate = mint(**_kwargs(), salt=s)
        if candidate not in seen:
            chosen = candidate
            break
    assert chosen is not None and chosen != canonical
