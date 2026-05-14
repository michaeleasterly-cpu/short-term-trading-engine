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


# ─── SPACs (blank-check companies) ──────────────────────────────────────


def test_classify_spac_by_name_acquisition_corp():
    """The 187/514 false-red on 2026-05-14: most of the "missing
    fundamentals" stocks were SPACs (AACO, ACAA, etc.). They get
    classified as 'spac' so the dashboard excludes them from the
    fundamentals denominator."""
    cls, inv, lev = _classify_from_name("Aimei Health Technology Acquisition Corp")
    assert cls == "spac"
    assert inv is None
    assert lev is None


def test_classify_spac_by_name_capital_corp():
    cls, _, _ = _classify_from_name("Acquisition Capital Corp")
    assert cls == "spac"


def test_classify_spac_by_ticker_suffix_u_unit():
    """Tickers ending in 'U' (units) trade alongside the underlying
    SPAC. AACOU, AEAQU, ACAAU — all SPAC units."""
    cls, _, _ = _classify_from_name("Aimei Health Technology Units", "AACOU")
    assert cls == "spac"


def test_classify_spac_by_ticker_suffix_w_warrant():
    """Tickers ending in 'W' / 'WS' / 'RW' are SPAC warrants."""
    cls, _, _ = _classify_from_name("Some Warrant Issue", "AEAQW")
    assert cls == "spac"


def test_classify_three_char_ticker_not_spac():
    """3-char tickers ending in W/U are common stocks (e.g., XPW),
    not SPAC derivatives. The suffix check requires len >= 4."""
    cls, _, _ = _classify_from_name("Stock Name Inc", "XPW")
    assert cls == "stock"


def test_classify_spac_name_beats_etf_name():
    """If a name has both ETF + Acquisition markers, SPAC wins
    (the classifier checks SPAC patterns first)."""
    cls, _, _ = _classify_from_name("Some Acquisition Corp ETF")
    assert cls == "spac"


def test_classify_spac_acquisition_iii_corp():
    """The 'Acquisition III Corp' pattern with Roman numerals between."""
    cls, _, _ = _classify_from_name("Black Spade Acquisition III Co")
    assert cls == "spac"


def test_classify_spac_class_a_ordinary_shares():
    """The 'Class A Ordinary Shares' SPAC trailer."""
    cls, _, _ = _classify_from_name("Cantor Equity Partners IV, Inc. Class A Ordinary Shares")
    assert cls == "spac"


# ─── Funds / preferred / notes / structured products ────────────────────


def test_classify_fund_notes_due():
    """Corporate notes are debt, not equity. FMP has no fundamentals."""
    cls, _, _ = _classify_from_name("CION Investment Corporation 7.50% Notes due 2031")
    assert cls == "fund"


def test_classify_fund_preferred_stock():
    cls, _, _ = _classify_from_name("OFS Credit Company, Inc. 7.875% Series F Term Preferred Stock")
    assert cls == "fund"


def test_classify_fund_depositary_shares():
    cls, _, _ = _classify_from_name("First Busey Corporation Depositary Shares")
    assert cls == "fund"


def test_classify_fund_structured_products():
    cls, _, _ = _classify_from_name("Synthetic Fixed-Income Securities STRATS 2006-2 Goldman Sachs")
    assert cls == "fund"


def test_classify_fund_bdc_investment_corp():
    """BDCs (Bain Capital GSS, Carlyle Credit, etc.) classify as 'fund'."""
    cls, _, _ = _classify_from_name("Bain Capital GSS Investment Corp.")
    assert cls == "fund"


def test_classify_etf_bill_fund():
    """Treasury bill funds aren't issuer-branded but classify as ETF."""
    cls, _, _ = _classify_from_name("The RBB Fund, Inc. F/m US Treasury 3 Month Bill Fund")
    assert cls == "etf"


# ─── Anchored issuer markers — must avoid false-positive on the parent ──


def test_classify_jpmorgan_chase_is_stock_not_etf():
    """The 'JPMorgan ' issuer prefix used to match JPMorgan Chase & Co
    (the bank), classifying it as an ETF. Real ETFs have an anchor
    word ('Fund', 'ETF', etc.) elsewhere in the name."""
    cls, _, _ = _classify_from_name("JPMorgan Chase & Co.")
    assert cls == "stock"


def test_classify_jpmorgan_etf_is_etf():
    """With the ETF anchor word, JPMorgan-branded products correctly
    classify as ETF."""
    cls, _, _ = _classify_from_name("JPMorgan Equity Premium Income ETF")
    assert cls == "etf"


def test_classify_pimco_corp_is_stock():
    """PIMCO without a fund anchor — would be the operating entity."""
    cls, _, _ = _classify_from_name("PIMCO Capital Inc")
    # 'Capital Inc' alone shouldn't trip SPAC (needs 'Acquisition' keyword)
    # or fund (needs notes/preferred/etc). Anchored ETF requires a
    # fund word. So this is stock.
    assert cls == "stock"


def test_classify_pimco_fund_is_etf():
    cls, _, _ = _classify_from_name("PIMCO Active Bond Exchange-Traded Fund")
    assert cls == "etf"
