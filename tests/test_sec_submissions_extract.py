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
    # FPFD = earliest filingDate across the FULL submission index (ALL
    # forms), NOT min(reportDate) of the primary form (spec §5.5/A5).
    # From the fixture the earliest filingDate is the 10-K filed
    # 2024-09-15 — that's the issuer's earliest filing in the window.
    assert meta["first_public_filing_date"] == date(2024, 9, 15)
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
    SEC-derived (earliest ``filingDate`` across the full submission
    index — spec §5.5/A5), NOT FMP's ``ipoDate`` field — FMP conflates
    SPAC-predecessor history and is unreliable for IPO dating.

    This test asserts the extractor never reads from any ``ipoDate``-
    shaped field, even if the synthetic payload smuggles one in.
    """
    payload = _submissions_for_10q()
    # Inject a malicious ipoDate in 2010 — extractor must IGNORE it.
    payload["ipoDate"] = "2010-01-01"
    meta = SECCompanyFactsAdapter.extract_filing_metadata(payload)
    # The earliest filingDate across ALL forms in the fixture is the
    # 10-K filed 2024-09-15 — never the smuggled ipoDate.
    assert meta["first_public_filing_date"] == date(2024, 9, 15)
    assert meta["first_public_filing_date"].year != 2010


def test_005e_fpfd_is_earliest_filing_date_across_all_forms() -> None:
    """FPFD = earliest ``filingDate`` across the FULL submission index,
    including NON-periodic forms (S-1 / 424B / 8-A), which predate the
    first periodic report (spec §5.5/A5).

    An S-1 registration filed before the first 10-Q is the issuer's
    true first public filing — FPFD must anchor on it, not on the
    earliest periodic-form filing.
    """
    payload = {
        "fiscalYearEnd": "1231",
        "filings": {"recent": {
            # An S-1 (2019) + 8-A (2019) precede the first 10-Q (2020).
            "form": ["10-Q", "10-K", "8-A12B", "S-1", "424B4"],
            "filingDate": [
                "2021-05-01", "2021-03-01",
                "2019-11-10", "2019-09-01", "2019-10-15",
            ],
            "reportDate": [
                "2021-03-31", "2020-12-31",
                "2019-11-10", "2019-09-01", "2019-10-15",
            ],
        }},
    }
    meta = SECCompanyFactsAdapter.extract_filing_metadata(payload)
    # Earliest filingDate across ALL forms is the S-1 on 2019-09-01.
    assert meta["first_public_filing_date"] == date(2019, 9, 1)
    # Primary periodic form is still classified correctly (10-K vs 10-Q
    # tie → most-recent-filing tie-break picks 10-Q).
    assert meta["document_type_primary"] == "10-Q"


def test_005f_later_filed_earlier_period_does_not_move_fpfd() -> None:
    """A restated/amended filing submitted LATER that reports an EARLIER
    period must NOT pull FPFD backward (no look-ahead via reportDate).

    FPFD is keyed on ``filingDate`` only — the date the entity actually
    made the filing — so a 2026-filed 10-K/A reporting a 2018 period
    leaves FPFD at the earliest ACTUAL filing date (2024-09-15).
    """
    payload = _submissions_for_10q()
    payload["filings"]["recent"]["form"].append("10-K/A")
    # Filed LATE (2026) but reports an OLD period (2018) — the old
    # reportDate must be ignored; the late filingDate must not become
    # FPFD either.
    payload["filings"]["recent"]["filingDate"].append("2026-06-01")
    payload["filings"]["recent"]["reportDate"].append("2018-06-30")
    meta = SECCompanyFactsAdapter.extract_filing_metadata(payload)
    # FPFD unchanged at the earliest ACTUAL filingDate (2024-09-15);
    # the 2018 reportDate did NOT move it backward.
    assert meta["first_public_filing_date"] == date(2024, 9, 15)


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


# ── TEST-008 — full_history pagination merges filings.files[] shards ──
# Spec PR #435 §12. Seven tests per the spec's tests_required list.


def _fake_resp(payload: dict, status: int = 200):
    """Build a minimal mock httpx response with .status_code + .json()."""
    from unittest.mock import MagicMock
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=payload)
    return r


@pytest.mark.asyncio
async def test_008a_full_history_false_preserves_existing_recent_only_behavior() -> None:
    """``full_history=False`` (default) issues exactly one HTTP call
    and returns the raw payload — no pagination. Existing callers that
    don't need deep history are unaffected."""
    from unittest.mock import AsyncMock, MagicMock

    base_payload = {
        "cik": "0000019617",
        "fiscalYearEnd": "1231",
        "filings": {
            "recent": {
                "form": ["10-Q"],
                "filingDate": ["2026-05-01"],
                "reportDate": ["2026-03-31"],
            },
            "files": [{"name": "CIK0000019617-submissions-001.json"}],
        },
    }
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=_fake_resp(base_payload))

    sec = SECCompanyFactsAdapter()
    sec._client = fake_client
    payload = await sec.get_submissions("0000019617")

    assert fake_client.get.await_count == 1
    # files[] NOT consumed — caller didn't ask for full history.
    assert payload["filings"]["files"] == [
        {"name": "CIK0000019617-submissions-001.json"},
    ]
    assert payload["filings"]["recent"]["form"] == ["10-Q"]
    assert "_shard_errors" not in payload


@pytest.mark.asyncio
async def test_008b_full_history_true_fetches_filings_files_shards() -> None:
    """``full_history=True`` fetches every shard listed in
    ``filings.files[]`` (one HTTP request per shard)."""
    from unittest.mock import AsyncMock, MagicMock

    base_payload = {
        "cik": "0000019617",
        "fiscalYearEnd": "1231",
        "filings": {
            "recent": {
                "form": ["10-Q"],
                "filingDate": ["2026-05-01"],
                "reportDate": ["2026-03-31"],
            },
            "files": [
                {"name": "CIK0000019617-submissions-001.json"},
                {"name": "CIK0000019617-submissions-002.json"},
                {"name": "CIK0000019617-submissions-003.json"},
            ],
        },
    }
    shard = {
        "form": ["10-Q"],
        "filingDate": ["2017-05-01"],
        "reportDate": ["2017-03-31"],
    }
    fake_client = MagicMock()
    fake_client.get = AsyncMock(side_effect=[
        _fake_resp(base_payload),
        _fake_resp(shard),
        _fake_resp(shard),
        _fake_resp(shard),
    ])

    sec = SECCompanyFactsAdapter()
    sec._client = fake_client
    await sec.get_submissions("0000019617", full_history=True)

    # 1 base + 3 shards = 4 HTTP calls.
    assert fake_client.get.await_count == 4


@pytest.mark.asyncio
async def test_008c_pagination_merges_recent_and_shard_filings() -> None:
    """Recent + each shard's parallel arrays are concatenated
    in-order in the returned ``filings.recent`` block, and ``files[]``
    is consumed (set to ``[]``)."""
    from unittest.mock import AsyncMock, MagicMock

    base_payload = {
        "cik": "0000019617",
        "fiscalYearEnd": "1231",
        "filings": {
            "recent": {
                "form": ["10-Q", "10-Q"],
                "filingDate": ["2026-05-01", "2026-02-01"],
                "reportDate": ["2026-03-31", "2025-12-31"],
            },
            "files": [
                {"name": "CIK0000019617-submissions-001.json"},
                {"name": "CIK0000019617-submissions-002.json"},
            ],
        },
    }
    shard_001 = {
        "form": ["10-Q", "10-Q"],
        "filingDate": ["2017-05-01", "2017-02-01"],
        "reportDate": ["2017-03-31", "2016-12-31"],
    }
    shard_002 = {
        "form": ["10-K", "10-Q"],
        "filingDate": ["1981-03-30", "1980-11-15"],
        "reportDate": ["1980-12-31", "1980-09-30"],
    }
    fake_client = MagicMock()
    fake_client.get = AsyncMock(side_effect=[
        _fake_resp(base_payload),
        _fake_resp(shard_001),
        _fake_resp(shard_002),
    ])

    sec = SECCompanyFactsAdapter()
    sec._client = fake_client
    payload = await sec.get_submissions("0000019617", full_history=True)

    recent = payload["filings"]["recent"]
    assert recent["form"] == [
        "10-Q", "10-Q",  # base
        "10-Q", "10-Q",  # shard 001
        "10-K", "10-Q",  # shard 002
    ]
    assert recent["reportDate"] == [
        "2026-03-31", "2025-12-31",
        "2017-03-31", "2016-12-31",
        "1980-12-31", "1980-09-30",
    ]
    assert recent["filingDate"] == [
        "2026-05-01", "2026-02-01",
        "2017-05-01", "2017-02-01",
        "1981-03-30", "1980-11-15",
    ]
    # files[] consumed so downstream callers don't re-paginate.
    assert payload["filings"]["files"] == []


@pytest.mark.asyncio
async def test_008d_mega_cap_fixture_computes_true_earliest_fpfd_not_recent_window_min() -> None:
    """JPM-style regression: when shards span decades, the extractor
    must compute ``first_public_filing_date`` from the merged history,
    not the recent shard's floor.

    Pre-pagination behaviour produced FPFD = min(reportDate within
    recent-shard) ≈ 2017+ for JPM. With pagination, the extractor sees
    the 1980 entry and returns it as FPFD."""
    from unittest.mock import AsyncMock, MagicMock

    base_payload = {
        "cik": "0000019617",
        "fiscalYearEnd": "1231",
        "filings": {
            "recent": {
                "form": ["10-Q", "10-K"],
                "filingDate": ["2026-05-01", "2025-09-15"],
                "reportDate": ["2026-03-31", "2025-06-30"],
            },
            "files": [
                {"name": "CIK0000019617-submissions-001.json"},
            ],
        },
    }
    shard_001 = {
        "form": ["10-K", "10-Q"],
        "filingDate": ["1981-03-30", "1980-11-15"],
        "reportDate": ["1980-12-31", "1980-09-30"],
    }
    fake_client = MagicMock()
    fake_client.get = AsyncMock(side_effect=[
        _fake_resp(base_payload),
        _fake_resp(shard_001),
    ])

    sec = SECCompanyFactsAdapter()
    sec._client = fake_client
    payload = await sec.get_submissions("0000019617", full_history=True)
    meta = SECCompanyFactsAdapter.extract_filing_metadata(payload)

    # FPFD = earliest filingDate across the FULL (merged) submission
    # index (spec §5.5/A5) — NOT min(reportDate). All filingDates:
    # 2026-05-01, 2025-09-15, 1981-03-30, 1980-11-15 → min = 1980-11-15
    # (the 1980 10-Q from the paginated shard). Pagination still beats
    # the recent-shard floor — which is the point of this regression.
    assert meta["first_public_filing_date"] == date(1980, 11, 15)
    # The 2025-09-15 recent-shard floor is NOT returned.
    assert meta["first_public_filing_date"] != date(2025, 9, 15)


@pytest.mark.asyncio
async def test_008e_no_shards_noop_uses_recent_only() -> None:
    """When ``filings.files[]`` is empty or missing, ``full_history=True``
    issues exactly one HTTP call and returns the payload as-is —
    functional no-op on small filers (the recent-IPO cohort)."""
    from unittest.mock import AsyncMock, MagicMock

    base_payload = {
        "cik": "0001234567",
        "fiscalYearEnd": "1231",
        "filings": {
            "recent": {
                "form": ["10-Q"],
                "filingDate": ["2026-05-01"],
                "reportDate": ["2026-03-31"],
            },
            "files": [],
        },
    }
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=_fake_resp(base_payload))

    sec = SECCompanyFactsAdapter()
    sec._client = fake_client
    payload = await sec.get_submissions("0001234567", full_history=True)

    assert fake_client.get.await_count == 1
    assert payload == base_payload


@pytest.mark.asyncio
async def test_008f_shard_fetch_error_degrades_gracefully() -> None:
    """If one shard returns 5xx, the merge skips it but keeps the
    partial result + lists the failed shard name in
    ``_shard_errors``."""
    from unittest.mock import AsyncMock, MagicMock

    base_payload = {
        "cik": "0000019617",
        "fiscalYearEnd": "1231",
        "filings": {
            "recent": {
                "form": ["10-Q"],
                "filingDate": ["2026-05-01"],
                "reportDate": ["2026-03-31"],
            },
            "files": [
                {"name": "CIK0000019617-submissions-001.json"},
                {"name": "CIK0000019617-submissions-002.json"},
            ],
        },
    }
    shard_001 = {
        "form": ["10-Q"],
        "filingDate": ["2017-05-01"],
        "reportDate": ["2017-03-31"],
    }
    # shard_002 errors out with 503.
    fake_client = MagicMock()
    fake_client.get = AsyncMock(side_effect=[
        _fake_resp(base_payload),
        _fake_resp(shard_001),
        _fake_resp({}, status=503),
    ])

    sec = SECCompanyFactsAdapter()
    sec._client = fake_client
    payload = await sec.get_submissions("0000019617", full_history=True)

    # All 3 fetches attempted; partial merge succeeds.
    assert fake_client.get.await_count == 3
    recent = payload["filings"]["recent"]
    assert recent["form"] == ["10-Q", "10-Q"]  # base + shard_001 only
    assert payload["_shard_errors"] == [
        "CIK0000019617-submissions-002.json",
    ]


def test_008g_backfill_sec_metadata_calls_get_submissions_full_history_true() -> None:
    """Sentinel: the ``_stage_backfill_sec_metadata`` caller must pass
    ``full_history=True`` to ``sec.get_submissions`` on the per-CIK
    HTTP fallback path. If anyone removes that argument (returning
    the recent-only behaviour), this test reds CI. Uses a
    whitespace-tolerant regex so the call can be formatted across
    multiple lines (e.g. nested under the bulk-mode branch)."""
    import re
    from pathlib import Path
    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "ops.py"
    ).read_text(encoding="utf-8")
    pattern = re.compile(
        r"sec\.get_submissions\(\s*cik\s*,\s*full_history\s*=\s*True\s*,?\s*\)"
    )
    assert pattern.search(source), (
        "_stage_backfill_sec_metadata must call "
        "`sec.get_submissions(cik, full_history=True)` so "
        "first_public_filing_date is computed across the issuer's "
        "complete SEC filing history. Spec PR #435 §10 + §13; "
        "removing the full_history=True kwarg silently regresses "
        "FPFD for ~999 long-lived issuers."
    )
