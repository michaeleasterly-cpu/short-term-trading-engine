"""Unit tests for the survivorship-free universe assembler + TKR-14 mint.

Plan 3 Phase 1. Pure-function coverage (no DB, no network, no asyncpg):

  * FPFD → lifetime_start; NEVER the 1900-01-01 sentinel.
  * survivorship-free: delisted tickers INCLUDED with lifetime_end set.
  * FMP-only gray zone (spec OQ-1): cik=None, discovery_source='F'.
  * SEC-first authority: FMP does NOT override a ticker SEC also covers.
  * TKR-14 mint + salt-collision retry (in-run uniqueness).
  * deterministic / idempotent: same inputs → same ids.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from tpcore.identity.tkr14 import (
    AssetClass,
    DiscoverySource,
    IPOVenue,
    decode,
    validate,
)
from tpcore.identity.universe_build import (
    FMPUniverseEntry,
    SECUniverseEntry,
    assemble_universe,
    mint_with_salt_retry,
    resolve_lifetime_start,
)

_SENTINEL = date(1900, 1, 1)
_NOW = datetime(2026, 6, 5, tzinfo=UTC)


# ── lifetime_start resolution ────────────────────────────────────────


def test_lifetime_start_prefers_sec_fpfd() -> None:
    got = resolve_lifetime_start(
        fpfd=date(2010, 3, 1), fmp_earliest=date(2012, 1, 1), now=_NOW
    )
    assert got == date(2010, 3, 1)


def test_lifetime_start_falls_back_to_fmp_when_no_fpfd() -> None:
    got = resolve_lifetime_start(
        fpfd=None, fmp_earliest=date(2012, 1, 1), now=_NOW
    )
    assert got == date(2012, 1, 1)


def test_lifetime_start_last_resort_is_now_not_sentinel() -> None:
    got = resolve_lifetime_start(fpfd=None, fmp_earliest=None, now=_NOW)
    assert got == _NOW.date()
    assert got != _SENTINEL


# ── assembly: lifetime_start never sentinel ──────────────────────────


def test_assemble_lifetime_start_from_fpfd_never_sentinel() -> None:
    sec = [
        SECUniverseEntry(
            ticker="AAPL", cik="0000320193", legal_name="Apple Inc.",
            first_public_filing_date=date(1994, 12, 12),
        ),
        SECUniverseEntry(
            ticker="MSFT", cik="0000789019", legal_name="Microsoft Corp.",
            first_public_filing_date=None,  # no FPFD → falls back, NOT sentinel
        ),
    ]
    rows = assemble_universe(sec_entries=sec, fmp_entries=[], now=_NOW)
    by_ticker = {r.ticker: r for r in rows}
    assert by_ticker["AAPL"].lifetime_start == date(1994, 12, 12)
    # MSFT had no FPFD + no FMP earliest → falls back to now(), never sentinel.
    assert by_ticker["MSFT"].lifetime_start == _NOW.date()
    for r in rows:
        assert r.lifetime_start != _SENTINEL


# ── survivorship-freeness: delisted INCLUDED ─────────────────────────


def test_assemble_includes_delisted_with_lifetime_end() -> None:
    sec = [
        SECUniverseEntry(
            ticker="LEH", cik="0000806085", legal_name="Lehman Brothers",
            first_public_filing_date=date(1994, 5, 1),
        ),
    ]
    fmp = [
        FMPUniverseEntry(
            ticker="LEH", company_name="Lehman Brothers Holdings",
            earliest_date=date(1994, 5, 1),
            delisted=True, delisting_date=date(2008, 9, 17),
        ),
    ]
    rows = assemble_universe(sec_entries=sec, fmp_entries=fmp, now=_NOW)
    assert len(rows) == 1
    leh = rows[0]
    # Delisted ticker is PRESENT in the universe (survivorship-free, G1/G3).
    assert leh.ticker == "LEH"
    assert leh.lifetime_end == date(2008, 9, 17)
    assert leh.lifetime_start == date(1994, 5, 1)
    assert leh.cik == "0000806085"


# ── FMP-only gray zone (OQ-1) ────────────────────────────────────────


def test_fmp_only_ticker_minted_with_null_cik_and_fmp_source() -> None:
    fmp = [
        FMPUniverseEntry(
            ticker="MICRO", company_name="Micro Cap Co",
            earliest_date=date(2019, 7, 1), country="US",
        ),
    ]
    rows = assemble_universe(sec_entries=[], fmp_entries=fmp, now=_NOW)
    assert len(rows) == 1
    micro = rows[0]
    assert micro.cik is None
    assert micro.source == "fmp"
    assert micro.discovery_source == DiscoverySource.FMP.value  # 'F'
    # lifetime_start from the FMP earliest date (no FPFD), never sentinel.
    assert micro.lifetime_start == date(2019, 7, 1)
    assert micro.lifetime_start != _SENTINEL
    # The minted id decodes to a SEC=... no, FMP discovery source.
    assert decode(micro.id).discovery_source == DiscoverySource.FMP


# ── SEC-first authority: FMP does NOT override ───────────────────────


def test_sec_wins_when_both_cover_ticker_fmp_does_not_duplicate() -> None:
    sec = [
        SECUniverseEntry(
            ticker="IBM", cik="0000051143", legal_name="IBM Corp",
            first_public_filing_date=date(1994, 1, 1),
        ),
    ]
    fmp = [
        FMPUniverseEntry(
            ticker="IBM", company_name="International Business Machines",
            earliest_date=date(1995, 1, 1),
        ),
    ]
    rows = assemble_universe(sec_entries=sec, fmp_entries=fmp, now=_NOW)
    # Exactly ONE row — SEC wins; FMP must not mint a duplicate (A8).
    assert len(rows) == 1
    ibm = rows[0]
    assert ibm.source == "sec"
    assert ibm.cik == "0000051143"
    assert ibm.discovery_source == DiscoverySource.SEC.value  # 'S'
    # SEC FPFD wins over the FMP earliest date.
    assert ibm.lifetime_start == date(1994, 1, 1)


def test_sec_row_adopts_fmp_delisting_metadata() -> None:
    """An SEC issuer FMP marks delisted gets a lifetime_end on the
    SEC-minted row — survivorship-free without an FMP duplicate."""
    sec = [
        SECUniverseEntry(
            ticker="OLD", cik="0000111111", legal_name="Old Co",
            first_public_filing_date=date(2000, 1, 1),
        ),
    ]
    fmp = [
        FMPUniverseEntry(
            ticker="OLD", company_name="Old Co",
            delisted=True, delisting_date=date(2015, 6, 30),
        ),
    ]
    rows = assemble_universe(sec_entries=sec, fmp_entries=fmp, now=_NOW)
    assert len(rows) == 1
    assert rows[0].source == "sec"
    assert rows[0].lifetime_end == date(2015, 6, 30)


# ── TKR-14 mint + salt-collision retry ───────────────────────────────


def test_mint_with_salt_retry_returns_valid_tkr14() -> None:
    seen: set[str] = set()
    new_id = mint_with_salt_retry(
        country="US", asset_class=AssetClass.STOCK, ipo_venue=IPOVenue.OTHER,
        discovery_source=DiscoverySource.SEC, cik="0000320193",
        legal_name="Apple Inc.", now=_NOW, seen_ids=seen,
    )
    assert validate(new_id)
    assert new_id in seen


def test_mint_with_salt_retry_resolves_collision() -> None:
    """If salt=0 collides with an already-seen id, the loop retries with
    salt=1,2,… until a unique id is produced (mint docstring contract)."""
    # Pre-seed seen_ids with the salt=0 id for this issuer so the first
    # attempt is forced to collide.
    from tpcore.identity.tkr14 import mint

    salt0 = mint(
        country="US", asset_class=AssetClass.STOCK, ipo_venue=IPOVenue.OTHER,
        discovery_source=DiscoverySource.SEC, cik="0000999999",
        legal_name="Collider Co", now=_NOW, salt=0,
    )
    seen = {salt0}
    new_id = mint_with_salt_retry(
        country="US", asset_class=AssetClass.STOCK, ipo_venue=IPOVenue.OTHER,
        discovery_source=DiscoverySource.SEC, cik="0000999999",
        legal_name="Collider Co", now=_NOW, seen_ids=seen,
    )
    # The retry produced a DIFFERENT, valid id (salt>=1).
    assert new_id != salt0
    assert validate(new_id)
    assert new_id in seen


def test_mint_with_salt_retry_raises_when_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pathological all-collide scenario surfaces a RuntimeError rather
    than looping forever."""
    import tpcore.identity.universe_build as ub

    monkeypatch.setattr(ub, "mint", lambda **_: "COLLIDING_ID_X")
    seen = {"COLLIDING_ID_X"}
    with pytest.raises(RuntimeError, match="collision unresolved"):
        ub.mint_with_salt_retry(
            country="US", asset_class=AssetClass.STOCK,
            ipo_venue=IPOVenue.OTHER, discovery_source=DiscoverySource.SEC,
            cik="0000000001", legal_name="X", now=_NOW, seen_ids=seen,
        )


# ── idempotency / determinism ────────────────────────────────────────


def test_assemble_is_deterministic_same_inputs_same_ids() -> None:
    sec = [
        SECUniverseEntry(
            ticker="AAPL", cik="0000320193", legal_name="Apple Inc.",
            first_public_filing_date=date(1994, 12, 12),
        ),
        SECUniverseEntry(
            ticker="MSFT", cik="0000789019", legal_name="Microsoft Corp.",
            first_public_filing_date=date(1992, 3, 1),
        ),
    ]
    rows_a = assemble_universe(sec_entries=sec, fmp_entries=[], now=_NOW)
    rows_b = assemble_universe(sec_entries=sec, fmp_entries=[], now=_NOW)
    ids_a = {r.ticker: r.id for r in rows_a}
    ids_b = {r.ticker: r.id for r in rows_b}
    assert ids_a == ids_b
    # All ids are unique within the run.
    assert len({r.id for r in rows_a}) == len(rows_a)


def test_assemble_all_ids_unique_across_sec_and_fmp() -> None:
    sec = [
        SECUniverseEntry(
            ticker="AAPL", cik="0000320193", legal_name="Apple Inc.",
            first_public_filing_date=date(1994, 12, 12),
        ),
    ]
    fmp = [
        FMPUniverseEntry(ticker="FOO", company_name="Foo", earliest_date=date(2020, 1, 1)),
        FMPUniverseEntry(ticker="BAR", company_name="Bar", earliest_date=date(2021, 1, 1)),
    ]
    rows = assemble_universe(sec_entries=sec, fmp_entries=fmp, now=_NOW)
    all_ids = [r.id for r in rows]
    assert len(all_ids) == len(set(all_ids)) == 3
    for r in rows:
        assert validate(r.id)
