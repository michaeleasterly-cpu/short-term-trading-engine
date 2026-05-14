"""Tests for ``tpcore.data.classify_tickers`` — name-based ETF classifier."""

from __future__ import annotations

from decimal import Decimal

from tpcore.data.classify_tickers import _classify_from_name

# ─── Stocks (no ETF marker) ─────────────────────────────────────────────


def test_classify_apple_is_stock():
    cls, inv, lev = _classify_from_name("Apple Inc")
    assert cls == "stock"
    assert inv is None
    assert lev is None


def test_classify_microsoft_is_stock():
    cls, inv, lev = _classify_from_name("Microsoft Corporation")
    assert cls == "stock"


def test_classify_empty_name_is_stock():
    """Defensive: empty name doesn't crash, defaults to stock."""
    cls, inv, lev = _classify_from_name("")
    assert cls == "stock"


# ─── ETFs ───────────────────────────────────────────────────────────────


def test_classify_ishares_is_etf():
    cls, inv, lev = _classify_from_name("iShares Core MSCI Emerging Markets ETF")
    assert cls == "etf"
    assert inv is False  # not inverse
    assert lev is None  # no leverage


def test_classify_spdr_is_etf():
    cls, inv, lev = _classify_from_name("SPDR S&P 500 ETF Trust")
    assert cls == "etf"
    assert inv is False


def test_classify_vanguard_etf():
    cls, _, _ = _classify_from_name("Vanguard Total Stock Market ETF")
    assert cls == "etf"


# ─── Inverse ETFs ───────────────────────────────────────────────────────


def test_classify_proshares_short_is_inverse():
    cls, inv, _ = _classify_from_name("ProShares Short S&P500")
    assert cls == "etf"
    assert inv is True


def test_classify_direxion_bear_is_inverse():
    cls, inv, _ = _classify_from_name("Direxion Daily S&P 500 Bear 3X Shares")
    assert cls == "etf"
    assert inv is True


def test_classify_proshares_ultrashort_is_inverse():
    cls, inv, _ = _classify_from_name("ProShares UltraShort S&P500")
    assert cls == "etf"
    assert inv is True


def test_classify_inverse_marker_in_name():
    cls, inv, _ = _classify_from_name("Some Inverse ETF Fund")
    assert cls == "etf"
    assert inv is True


# ─── Leverage detection ─────────────────────────────────────────────────


def test_classify_2x_leverage():
    cls, _, lev = _classify_from_name("ProShares Ultra QQQ 2x Shares")
    assert cls == "etf"
    assert lev == Decimal("2")


def test_classify_3x_leverage():
    # Realistic naming convention: leverage marker + Bear keyword in
    # the middle of the name (matching Direxion's actual format).
    cls, inv, lev = _classify_from_name("Direxion Daily 3x S&P 500 Bear Shares")
    assert cls == "etf"
    assert inv is True
    assert lev == Decimal("3")


def test_classify_no_leverage_marker_returns_none():
    """ETFs without an explicit Nx marker leave leverage None (=1x)."""
    cls, _, lev = _classify_from_name("iShares Core MSCI Emerging Markets ETF")
    assert cls == "etf"
    assert lev is None
