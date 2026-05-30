"""P0-002 — SEC submissions.json metadata extractor (offline tests).

Tests:
  * TEST-004 extract_filing_metadata_document_type — 10-Q vs 20-F vs 40-F
  * TEST-005 extract_filing_metadata_period_dates — first/last filing
  * TEST-006 first_public_filing_date_NOT_from_fmp_ipo — guards against
    the operator's hard rule (no FMP ipoDate aliasing).
"""
from __future__ import annotations

import os
from datetime import date

import pytest

# The adapter requires SEC_EDGAR_USER_AGENT to instantiate. Set a
# placeholder so ``SECCompanyFactsAdapter()`` doesn't raise — we only
# test the pure static extractor here, no HTTP.
os.environ.setdefault("SEC_EDGAR_USER_AGENT", "STE-test test@example.com")

from tpcore.sec.companyfacts_adapter import (  # noqa: E402
    SECCompanyFactsAdapter,
    _parse_fiscal_year_end_mmdd,
)


def _submissions_for_10q(*, fy: str = "1231") -> dict:
    """Synthetic submissions payload heavy on 10-Q (US filer)."""
    return {
        "cik": "0000320193",
        "fiscalYearEnd": fy,
        "filings": {
            "recent": {
                "form": [
                    "10-Q", "10-Q", "10-Q", "10-K", "10-Q", "10-K",
                    "8-K", "8-K", "4", "4", "4",
                ],
                "filingDate": [
                    "2026-05-01", "2026-02-01", "2025-11-01",
                    "2025-09-15", "2025-08-01", "2024-09-15",
                    "2026-05-20", "2026-03-15", "2026-05-10",
                    "2026-04-01", "2026-03-01",
                ],
                "reportDate": [
                    "2026-03-31", "2025-12-31", "2025-09-30",
                    "2025-06-30", "2025-06-30", "2024-06-30",
                    "2026-05-20", "2026-03-15", "2026-05-10",
                    "2026-04-01", "2026-03-01",
                ],
            },
        },
    }


def _submissions_for_20f() -> dict:
    """Foreign private issuer — 20-F annual + 6-K interim event."""
    return {
        "cik": "0000842180",
        "fiscalYearEnd": "1231",
        "filings": {
            "recent": {
                "form": [
                    "20-F", "20-F", "20-F",
                    "6-K", "6-K", "6-K", "6-K",
                ],
                "filingDate": [
                    "2026-04-15", "2025-04-15", "2024-04-15",
                    "2026-05-26", "2026-03-15", "2025-11-01",
                    "2025-08-15",
                ],
                "reportDate": [
                    "2025-12-31", "2024-12-31", "2023-12-31",
                    "2026-05-26", "2026-03-15", "2025-11-01",
                    "2025-08-15",
                ],
            },
        },
    }


def _submissions_for_40f() -> dict:
    """Canadian MJDS filer — 40-F annual."""
    return {
        "cik": "0001234567",
        "fiscalYearEnd": "1231",
        "filings": {
            "recent": {
                "form": ["40-F", "40-F", "6-K", "6-K"],
                "filingDate": [
                    "2026-03-30", "2025-03-30", "2026-02-15", "2025-11-01",
                ],
                "reportDate": [
                    "2025-12-31", "2024-12-31", "2026-02-15", "2025-11-01",
                ],
            },
        },
    }


def test_004_extract_filing_metadata_document_type_10q() -> None:
    meta = SECCompanyFactsAdapter.extract_filing_metadata(
        _submissions_for_10q(),
    )
    assert meta["document_type_primary"] == "10-Q"
    # Histogram includes non-periodic forms too.
    hist = meta["document_type_history"]
    assert hist["10-Q"] == 4
    assert hist["10-K"] == 2
    assert hist["8-K"] == 2
    assert hist["4"] == 3


def test_004b_extract_filing_metadata_document_type_20f() -> None:
    meta = SECCompanyFactsAdapter.extract_filing_metadata(
        _submissions_for_20f(),
    )
    # 20-F (3 occurrences) wins over 6-K (4 occurrences) because 6-K
    # is NOT in the periodic-forms set — only 20-F/40-F/10-K/10-Q are.
    assert meta["document_type_primary"] == "20-F"


def test_004c_extract_filing_metadata_document_type_40f() -> None:
    meta = SECCompanyFactsAdapter.extract_filing_metadata(
        _submissions_for_40f(),
    )
    assert meta["document_type_primary"] == "40-F"


def test_005_extract_filing_metadata_period_dates() -> None:
    meta = SECCompanyFactsAdapter.extract_filing_metadata(
        _submissions_for_10q(),
    )
    # first_public_filing_date = min(reportDate) over rows where
    # form matches the primary 10-Q. From the fixture:
    # 10-Q reportDates: 2026-03-31, 2025-12-31, 2025-09-30, 2025-06-30
    # → min = 2025-06-30
    assert meta["first_public_filing_date"] == date(2025, 6, 30)
    # last_filing_date = max(filingDate) across ALL forms. Latest is
    # the 8-K on 2026-05-20.
    assert meta["last_filing_date"] == date(2026, 5, 20)


def test_005b_fiscal_year_end_month_parse() -> None:
    # September 26 (Apple) → month 9
    meta = SECCompanyFactsAdapter.extract_filing_metadata({
        "fiscalYearEnd": "0926",
        "filings": {"recent": {
            "form": ["10-Q"],
            "filingDate": ["2026-05-01"],
            "reportDate": ["2026-03-31"],
        }},
    })
    assert meta["fiscal_year_end_month"] == 9


def test_005c_fiscal_year_end_invalid_returns_none() -> None:
    assert _parse_fiscal_year_end_mmdd(None) is None
    assert _parse_fiscal_year_end_mmdd("") is None
    assert _parse_fiscal_year_end_mmdd("12") is None       # too short
    assert _parse_fiscal_year_end_mmdd("13xx") is None     # not a month
    assert _parse_fiscal_year_end_mmdd("1331") is None     # month 13
    assert _parse_fiscal_year_end_mmdd("ABCD") is None     # not int
    assert _parse_fiscal_year_end_mmdd("0926") == 9


def test_005d_empty_filings_returns_none_fields() -> None:
    meta = SECCompanyFactsAdapter.extract_filing_metadata({
        "fiscalYearEnd": "1231",
        "filings": {"recent": {"form": [], "filingDate": [], "reportDate": []}},
    })
    assert meta["document_type_primary"] is None
    assert meta["document_type_history"] is None
    assert meta["first_public_filing_date"] is None
    assert meta["last_filing_date"] is None
    # fiscal_year_end_month STILL parsed from top-level field.
    assert meta["fiscal_year_end_month"] == 12


def test_006_first_public_filing_date_NOT_from_fmp_ipo() -> None:
    """Operator hard rule: ``first_public_filing_date`` must be
    SEC-derived (min(reportDate) over primary periodic filings), NOT
    FMP's ``ipoDate`` field — FMP conflates SPAC-predecessor history
    and is unreliable for IPO dating.

    This test asserts the extractor never reads from any ``ipoDate``-
    shaped field, even if the synthetic payload smuggles one in.
    """
    payload = _submissions_for_10q()
    # Inject a malicious ipoDate in 2010 — extractor must IGNORE it.
    payload["ipoDate"] = "2010-01-01"
    meta = SECCompanyFactsAdapter.extract_filing_metadata(payload)
    # The 10-Q earliest reportDate in the fixture is 2025-06-30.
    assert meta["first_public_filing_date"] == date(2025, 6, 30)
    assert meta["first_public_filing_date"].year != 2010


def test_006b_amendment_collapses_to_base() -> None:
    """10-Q/A counts toward 10-Q for primary-type classification."""
    payload = {
        "fiscalYearEnd": "1231",
        "filings": {"recent": {
            "form": ["10-Q/A", "10-Q/A", "10-K", "10-K"],
            "filingDate": [
                "2026-03-01", "2025-12-01", "2025-09-15", "2024-09-15",
            ],
            "reportDate": [
                "2026-01-31", "2025-10-31", "2025-06-30", "2024-06-30",
            ],
        }},
    }
    meta = SECCompanyFactsAdapter.extract_filing_metadata(payload)
    # 10-Q/A (2) collapses to 10-Q (2) which ties 10-K (2). Tie-break
    # picks the form whose MOST RECENT filing is newest. 10-Q/A latest
    # filingDate = 2026-03-01 vs 10-K latest = 2025-09-15 → 10-Q wins.
    assert meta["document_type_primary"] == "10-Q"


@pytest.mark.asyncio
async def test_007_get_submissions_requires_context_manager() -> None:
    """The adapter must be used as an async context manager."""
    sec = SECCompanyFactsAdapter()  # _client is None
    with pytest.raises(RuntimeError, match="context manager"):
        await sec.get_submissions("0000320193")
