"""Tests for the clean-slate staging-spine pure mint layer.

Spec: ``docs/superpowers/specs/2026-06-08-data-foundation-systemic-fix-
design.md`` §7-B. Plan A1/A2.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from tpcore.identity.staging_spine_build import (
    SpineBuildInput,
    assemble_spine,
    mint_era_id,
    resolve_lifetime_end,
    resolve_lifetime_start,
)
from tpcore.identity.tkr14 import validate

NOW = datetime(2026, 6, 8, tzinfo=UTC)


# ── resolve_lifetime_start ──────────────────────────────────────────────
def test_lifetime_start_bar_earlier_than_fpfd_wins() -> None:
    # A security that traded BEFORE its first SEC filing (foreign/OTC/pre-IPO):
    # the earliest real-day evidence (the bar) wins.
    assert resolve_lifetime_start(
        fpfd=date(2012, 5, 18), first_bar=date(2011, 5, 23),
        fmp_earliest=None, now=NOW,
    ) == date(2011, 5, 23)


def test_lifetime_start_fpfd_earlier_than_bar_wins() -> None:
    assert resolve_lifetime_start(
        fpfd=date(2005, 5, 6), first_bar=date(2011, 5, 23),
        fmp_earliest=None, now=NOW,
    ) == date(2005, 5, 6)


def test_lifetime_start_fmp_only() -> None:
    assert resolve_lifetime_start(
        fpfd=None, first_bar=None, fmp_earliest=date(2015, 1, 2), now=NOW,
    ) == date(2015, 1, 2)


def test_lifetime_start_no_evidence_falls_back_to_now_never_sentinel() -> None:
    out = resolve_lifetime_start(
        fpfd=None, first_bar=None, fmp_earliest=None, now=NOW
    )
    assert out == NOW.date()
    assert out != date(1900, 1, 1)


def test_lifetime_start_drops_synthetic_jan1_fmp_when_realday_exists() -> None:
    # The ABR rot: a dirty Jan-1 fmp_earliest from a DIFFERENT entity-era must
    # NOT win the min() over the real SEC FPFD.
    out = resolve_lifetime_start(
        fpfd=date(2003, 7, 11), first_bar=date(2011, 5, 2),
        fmp_earliest=date(2002, 1, 1), now=NOW,
    )
    assert out == date(2003, 7, 11)  # SEC FPFD, not the Jan-1


def test_lifetime_start_uses_jan1_only_when_sole_evidence() -> None:
    # A genuine no-other-evidence Jan-1 is still a real date (never 1900).
    out = resolve_lifetime_start(
        fpfd=None, first_bar=None, fmp_earliest=date(2015, 1, 1), now=NOW,
    )
    assert out == date(2015, 1, 1)


def test_lifetime_start_jan1_fpfd_dropped_for_realday_bar() -> None:
    out = resolve_lifetime_start(
        fpfd=date(2010, 1, 1), first_bar=date(2010, 6, 15),
        fmp_earliest=None, now=NOW,
    )
    assert out == date(2010, 6, 15)


# ── resolve_lifetime_end ────────────────────────────────────────────────
def test_lifetime_end_still_trading_open() -> None:
    assert resolve_lifetime_end(
        sec_delisting_date=None, fmp_delisting_date=None,
        known_delisting_date=None, last_bar=date(2026, 6, 5),
        lifetime_start=date(2020, 1, 1), still_trading=True, ticker="X",
    ) is None


def test_lifetime_end_extended_past_last_bar_when_evidence_stale() -> None:
    # Vendor delisting predates a real bar → extend past the bar (half-open).
    assert resolve_lifetime_end(
        sec_delisting_date=date(2022, 1, 1), fmp_delisting_date=None,
        known_delisting_date=None, last_bar=date(2026, 6, 5),
        lifetime_start=date(2010, 1, 1), still_trading=False, ticker="X",
    ) == date(2026, 6, 6)


def test_lifetime_end_evidence_after_last_bar_wins() -> None:
    assert resolve_lifetime_end(
        sec_delisting_date=date(2023, 7, 21), fmp_delisting_date=None,
        known_delisting_date=None, last_bar=date(2023, 7, 10),
        lifetime_start=date(1994, 1, 1), still_trading=False, ticker="FISV",
    ) == date(2023, 7, 21)


def test_lifetime_end_precedence_sec_over_fmp_over_known() -> None:
    assert resolve_lifetime_end(
        sec_delisting_date=date(2022, 1, 5), fmp_delisting_date=date(2022, 2, 1),
        known_delisting_date=date(2022, 3, 1), last_bar=None,
        lifetime_start=date(2010, 1, 1), still_trading=False, ticker="X",
    ) == date(2022, 1, 5)


def test_lifetime_end_dropped_when_le_start() -> None:
    assert resolve_lifetime_end(
        sec_delisting_date=date(2010, 1, 1), fmp_delisting_date=None,
        known_delisting_date=None, last_bar=None,
        lifetime_start=date(2020, 1, 1), still_trading=False, ticker="X",
    ) is None


# ── assemble_spine ──────────────────────────────────────────────────────
def test_assemble_sets_current_ticker_and_uppercases() -> None:
    secs = assemble_spine(
        [SpineBuildInput(ticker="aapl", asset_class="stock", cik="0000320193",
                         fpfd=date(1994, 1, 26), first_bar=date(2000, 1, 3),
                         last_bar=date(2026, 6, 5), still_trading=True)],
        now=NOW,
    )
    assert len(secs) == 1
    assert secs[0].ticker == "AAPL"
    assert secs[0].current_ticker == "AAPL"  # never NULL
    assert secs[0].lifetime_start == date(1994, 1, 26)
    assert secs[0].lifetime_end is None
    assert validate(secs[0].id)


def test_assemble_no_synthetic_jan1_when_bar_evidence() -> None:
    secs = assemble_spine(
        [SpineBuildInput(ticker="X", asset_class="stock", cik=None,
                         first_bar=date(2015, 3, 14), last_bar=date(2026, 1, 2),
                         still_trading=True)],
        now=NOW,
    )
    assert secs[0].lifetime_start == date(2015, 3, 14)
    assert not (secs[0].lifetime_start.month == 1
                and secs[0].lifetime_start.day == 1)


def test_assemble_window_covers_bar_span_by_construction() -> None:
    # P3 guarantee: lifetime_start <= first_bar AND end open/after last_bar.
    inp = SpineBuildInput(
        ticker="DELISTED", asset_class="stock", cik="0000999999",
        fpfd=date(2008, 6, 1), first_bar=date(2009, 1, 5),
        last_bar=date(2020, 3, 10), fmp_delisting_date=date(2019, 1, 1),
        still_trading=False,
    )
    s = assemble_spine([inp], now=NOW)[0]
    assert s.lifetime_start <= inp.first_bar
    assert s.lifetime_end is not None and s.lifetime_end > inp.last_bar


def test_mint_distinct_ids_for_same_cik_many_symbols() -> None:
    # The collision regression: many ETF symbols under ONE trust CIK must each
    # mint a DISTINCT, deterministic id (the per-symbol seed fix).
    seen: set[str] = set()
    ids = [
        mint_era_id(
            inp=SpineBuildInput(ticker=f"ETF{i}", asset_class="etf",
                                cik="0001100663"),
            now=NOW, seen_ids=seen,
        )
        for i in range(120)
    ]
    assert len(set(ids)) == 120  # all distinct
    assert all(validate(i) for i in ids)


def test_mint_deterministic_across_runs() -> None:
    inp = SpineBuildInput(ticker="SPY", asset_class="etf", cik="0000884394")
    a = mint_era_id(inp=inp, now=NOW, seen_ids=set())
    b = mint_era_id(inp=inp, now=NOW, seen_ids=set())
    assert a == b


def test_assemble_skips_empty_ticker() -> None:
    secs = assemble_spine(
        [SpineBuildInput(ticker="   ", asset_class="stock", cik="0000000001",
                         first_bar=date(2020, 1, 1), still_trading=True)],
        now=NOW,
    )
    assert secs == []


@pytest.mark.parametrize(
    "asset_class", ["stock", "etf", "etn", "fund", "reit", "adr", "spac"],
)
def test_assemble_all_asset_classes_mint_valid(asset_class: str) -> None:
    s = assemble_spine(
        [SpineBuildInput(ticker="T", asset_class=asset_class, cik="0000000002",
                         first_bar=date(2020, 1, 1), still_trading=True)],
        now=NOW,
    )[0]
    assert s.asset_class == asset_class
    assert validate(s.id)
