"""P2a — SEC Form 25 / Form 15 lifecycle-events extractor tests (offline).

Hermetic tests of the extractor + URL builder. NO database, NO network.

Coverage matrix (expert design review §10):
  TEST-P2-A   extractor: no Form 25 / Form 15 → derived_state=None
  TEST-P2-B   extractor: Form 25 only → derived_state='delist_effective'
  TEST-P2-C   extractor: Form 15 only → derived_state='deregistered'
  TEST-P2-D   extractor: Form 25 then Form 15 → 'deregistered' wins
  TEST-P2-E   extractor: Form 15 variants (15, 15-12G, 15-12B, 15F, 15-15D)
              all route to 'deregistered'
  TEST-P2-F   extractor: derived_event_date prefers report_date over filing_date
  TEST-P2-G   URL builder: valid CIK + accession → canonical SEC Archives URL
  TEST-P2-H   URL builder: malformed accession → NULL (no guess)
  TEST-P2-I   URL builder: NULL inputs → NULL
  TEST-P2-J   provenance precedence dict — manual top, sec_form_15 > 25 > others
  TEST-P2-K   name-collision sentinel — ``lifecycle_state`` not in
              ticker_classifications schema (P0 + P1 keep the column on
              ticker_classifications named ``issuer_lifecycle_state``)
  TEST-P2-L   stage registration — ``backfill_sec_lifecycle`` is in
              _STAGE_SPECS
"""
from __future__ import annotations

import os
from datetime import date

import pytest

os.environ.setdefault("SEC_EDGAR_USER_AGENT", "STE-test test@example.com")

from tpcore.sec.companyfacts_adapter import (  # noqa: E402
    _FORM_15_VARIANTS,
    _FORM_25_VARIANTS,
    _LIFECYCLE_FORM_VARIANTS,
    SECCompanyFactsAdapter,
    _build_sec_filing_url,
    _extract_lifecycle_events,
)


def _submissions(forms_and_dates: list[tuple[str, str, str, str]]) -> dict:
    """Build a synthetic submissions payload.

    ``forms_and_dates`` items: ``(form, filing_date_iso, report_date_iso,
    accession_number)``.
    """
    return {
        "filings": {
            "recent": {
                "form": [t[0] for t in forms_and_dates],
                "filingDate": [t[1] for t in forms_and_dates],
                "reportDate": [t[2] for t in forms_and_dates],
                "accessionNumber": [t[3] for t in forms_and_dates],
            },
        },
    }


# ─── A. extractor returns None when no Form 25 / Form 15 present ──

def test_p2_a_no_form_25_or_15_returns_none() -> None:
    result = _extract_lifecycle_events(_submissions([
        ("10-Q", "2025-05-01", "2025-03-31", "0000320193-25-000111"),
        ("8-K",  "2025-06-15", "2025-06-15", "0000320193-25-000222"),
    ]), cik="0000320193")
    assert result["derived_state"] is None
    assert result["derived_source"] is None
    assert result["derived_event_date"] is None
    assert result["form_25_events"] == []
    assert result["form_15_events"] == []


# ─── B. Form 25 only → delist_effective ───────────────────────────

def test_p2_b_form_25_only_routes_to_delist_effective() -> None:
    result = _extract_lifecycle_events(_submissions([
        ("25", "2024-03-15", "2024-03-25", "0001353283-24-000111"),
    ]), cik="0001353283")
    assert result["derived_state"] == "delist_effective"
    assert result["derived_source"] == "sec_form_25"
    # Prefers report_date over filing_date.
    assert result["derived_event_date"] == date(2024, 3, 25)
    assert len(result["form_25_events"]) == 1
    assert result["form_15_events"] == []


# ─── C. Form 15 only → deregistered ───────────────────────────────

def test_p2_c_form_15_only_routes_to_deregistered() -> None:
    result = _extract_lifecycle_events(_submissions([
        ("15-12G", "2023-10-23", "2023-10-23", "0000718877-23-000111"),
    ]), cik="0000718877")
    assert result["derived_state"] == "deregistered"
    assert result["derived_source"] == "sec_form_15"
    assert len(result["form_15_events"]) == 1
    assert result["form_25_events"] == []


# ─── D. Form 25 then Form 15 → deregistered wins ──────────────────

def test_p2_d_form_25_then_form_15_deregistered_wins() -> None:
    """The two-step lifecycle: Form 25 first (delisting notice), then
    Form 15 (deregistration) weeks later. Both events are extracted
    and recorded; the projection MUST flip to 'deregistered'."""
    result = _extract_lifecycle_events(_submissions([
        ("25",     "2023-10-15", "2023-10-25", "0000718877-23-000222"),
        ("15-12G", "2023-10-23", "2023-10-23", "0000718877-23-000333"),
    ]), cik="0000718877")
    assert result["derived_state"] == "deregistered"
    assert result["derived_source"] == "sec_form_15"
    assert len(result["form_25_events"]) == 1
    assert len(result["form_15_events"]) == 1
    # Both events are kept — append-only audit trail in the event log.


# ─── E. Form 15 variants all route to deregistered ────────────────

@pytest.mark.parametrize(
    "variant",
    ["15", "15-12G", "15-12B", "15F", "15-15D"],
)
def test_p2_e_form_15_variants_all_route_to_deregistered(
    variant: str,
) -> None:
    result = _extract_lifecycle_events(_submissions([
        (variant, "2024-01-15", "2024-01-15", "0001234567-24-000111"),
    ]), cik="0001234567")
    assert result["derived_state"] == "deregistered"
    assert result["form_15_events"][0]["form"] == variant


@pytest.mark.parametrize("variant", ["25", "25-NSE"])
def test_p2_e2_form_25_variants_all_route_to_delist_effective(
    variant: str,
) -> None:
    result = _extract_lifecycle_events(_submissions([
        (variant, "2024-01-15", "2024-01-25", "0001234567-24-000111"),
    ]), cik="0001234567")
    assert result["derived_state"] == "delist_effective"
    assert result["form_25_events"][0]["form"] == variant


def test_p2_e3_variant_sets_no_overlap() -> None:
    assert not (_FORM_25_VARIANTS & _FORM_15_VARIANTS)
    assert _LIFECYCLE_FORM_VARIANTS == _FORM_25_VARIANTS | _FORM_15_VARIANTS


# ─── F. event_date prefers report_date over filing_date ───────────

def test_p2_f_event_date_prefers_report_date() -> None:
    """When a Form 25 carries both filing_date and report_date, the
    projection uses report_date (the issuer's claimed effective date,
    which is what the validator wants for tradeability reasoning)."""
    result = _extract_lifecycle_events(_submissions([
        ("25", "2024-03-15", "2024-03-25", "0001353283-24-000111"),
    ]), cik="0001353283")
    assert result["derived_event_date"] == date(2024, 3, 25)


def test_p2_f2_event_date_falls_back_to_filing_date() -> None:
    """If a Form 25 has NO report_date, the projection uses the
    filing_date — never None when an event is present."""
    payload = _submissions([
        ("25", "2024-03-15", "", "0001353283-24-000111"),
    ])
    # Clear out the empty report_date so it parses as None.
    payload["filings"]["recent"]["reportDate"] = [None]
    result = _extract_lifecycle_events(payload, cik="0001353283")
    assert result["derived_event_date"] == date(2024, 3, 15)


# ─── G. URL builder canonical SEC Archives path ───────────────────

def test_p2_g_url_builder_valid() -> None:
    url = _build_sec_filing_url("0000320193", "0000320193-25-000123")
    assert url == (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019325000123/0000320193-25-000123-index.htm"
    )


def test_p2_g2_url_builder_strips_cik_leading_zeros() -> None:
    """CIK is stored zero-padded but the Archives path uses the
    integer (no leading zeros)."""
    url = _build_sec_filing_url("0000000800", "0000000800-22-000111")
    assert url is not None
    assert "/data/800/" in url


# ─── H. URL builder malformed accession → NULL ────────────────────

@pytest.mark.parametrize(
    "bad_accession",
    [
        "BADACC",
        "1234-56-789012",       # parts lengths wrong
        "12345678901-23-456789",  # part 1 length wrong (11 digits)
        "1234567890-2-456789",   # part 2 length wrong (1 digit)
        "1234567890-23-45678",   # part 3 length wrong (5 digits)
        "ABCDEFGHIJ-12-345678",  # not all-digit
        "",
        "0000320193_25_000123",  # underscores not dashes
    ],
)
def test_p2_h_url_builder_rejects_malformed_accession(
    bad_accession: str,
) -> None:
    assert _build_sec_filing_url("0000320193", bad_accession) is None


# ─── I. URL builder null inputs → null ────────────────────────────

@pytest.mark.parametrize("cik", [None, "", "abc"])
def test_p2_i_url_builder_rejects_bad_cik(cik: object) -> None:
    assert _build_sec_filing_url(cik, "0000320193-25-000123") is None  # type: ignore[arg-type]


def test_p2_i2_url_builder_rejects_zero_cik() -> None:
    assert _build_sec_filing_url("0000000000", "0000320193-25-000123") is None


# ─── J. provenance precedence dict ────────────────────────────────

def test_p2_j_precedence_dict_ordering() -> None:
    from scripts.ops import _LIFECYCLE_SOURCE_PRECEDENCE as p

    # manual top — never overwritten.
    assert p["manual"] > p["sec_form_15"]
    # SEC strongest evidence first (deregistration > delist > 8-K disclosure).
    assert p["sec_form_15"] > p["sec_form_25"]
    assert p["sec_form_25"] > p["sec_form_8k"]
    # Vendor sources lowest priority.
    assert p["sec_form_8k"] > p["alpaca_asset_status"]
    assert p["alpaca_asset_status"] > p["fmp_profile"]


# ─── K. name-collision sentinel ──────────────────────────────────

def test_p2_k_no_lifecycle_state_collision_on_ticker_classifications() -> None:
    """The P2a column on ``ticker_classifications`` is
    ``issuer_lifecycle_state`` — NEVER ``lifecycle_state``. The latter
    is the engine-SDLC concept on ``EngineProfile.lifecycle_state``
    (tpcore/engine_profile.py). Distinct domains; the column-name
    sentinel protects against an accidental rename or new column
    that re-introduces the collision."""
    from pathlib import Path
    repo_root = Path(__file__).resolve().parent.parent
    mig_path = (
        repo_root / "platform" / "migrations" / "versions"
        / "20260530_0300_issuer_lifecycle_evidence_foundation.py"
    )
    assert mig_path.exists(), f"migration file missing: {mig_path}"
    src = mig_path.read_text(encoding="utf-8")
    # The migration MUST use the prefixed name everywhere it writes
    # to ticker_classifications.
    assert "issuer_lifecycle_state " in src or "issuer_lifecycle_state\n" in src
    # The unprefixed name MUST NOT appear as a target column.
    forbidden = [
        "ADD COLUMN IF NOT EXISTS lifecycle_state ",
        "ADD COLUMN IF NOT EXISTS lifecycle_state\n",
    ]
    for needle in forbidden:
        assert needle not in src, (
            "ticker_classifications must use issuer_lifecycle_state — "
            "the unprefixed name collides with EngineProfile.lifecycle_state"
        )


# ─── L. backfill stage registration ──────────────────────────────

def test_p2_l_backfill_stage_registered() -> None:
    from scripts import ops
    names = {n for n, _, _ in ops._STAGE_SPECS}
    assert "backfill_sec_lifecycle" in names


# ─── M. SECCompanyFactsAdapter.extract_lifecycle_events wrapper ──

def test_p2_m_adapter_static_wrapper_works() -> None:
    """The static method on the adapter class mirrors the module-level
    helper — same return shape, same routing."""
    payload = _submissions([
        ("25", "2024-03-15", "2024-03-25", "0001353283-24-000111"),
    ])
    via_adapter = SECCompanyFactsAdapter.extract_lifecycle_events(
        payload, cik="0001353283",
    )
    via_module = _extract_lifecycle_events(payload, cik="0001353283")
    assert via_adapter == via_module


# ─── N. empty / malformed submissions are tolerated ──────────────

def test_p2_n_empty_submissions_returns_none() -> None:
    result = _extract_lifecycle_events({}, cik="0001353283")
    assert result["derived_state"] is None
    assert result["form_25_events"] == []
    assert result["form_15_events"] == []


def test_p2_n2_missing_filings_block_returns_none() -> None:
    result = _extract_lifecycle_events(
        {"filings": {"recent": {}}}, cik="0001353283",
    )
    assert result["derived_state"] is None


# ─── O. cache layer — bulk-before-API-crawl rule ──────────────────


@pytest.mark.asyncio
async def test_p2_o_cache_hit_skips_http(tmp_path) -> None:
    """If a CIK's submissions.json is already on disk, the cached
    getter MUST return it without hitting SEC. Operator standing
    rule: bulk-file before API crawl — never re-pull what you have.

    We stub the HTTP fallback to RAISE so any network call would
    surface as a test failure."""
    import json
    cache_dir = tmp_path / "sec_submissions"
    cache_dir.mkdir()
    (cache_dir / "CIK0000320193.json").write_text(json.dumps({
        "filings": {"recent": {"form": [], "filingDate": [],
                                "reportDate": [], "accessionNumber": []}},
        "fiscalYearEnd": "1231",
    }), encoding="utf-8")

    sec = SECCompanyFactsAdapter()
    # No HTTP context entered — get_submissions would raise RuntimeError
    # if it were called. The cache hit must short-circuit.
    payload = await sec.get_submissions_cached(
        "320193", cache_dir=str(cache_dir),
    )
    assert payload is not None
    assert payload.get("fiscalYearEnd") == "1231"


@pytest.mark.asyncio
async def test_p2_o2_cache_404_sentinel_short_circuits(tmp_path) -> None:
    """A cached SEC 404 (sentinel: ``{"__sec_404__": true}``) returns
    None without re-hitting SEC. Avoids hammering known-missing CIKs."""
    import json
    cache_dir = tmp_path / "sec_submissions"
    cache_dir.mkdir()
    (cache_dir / "CIK0000999999.json").write_text(
        json.dumps({"__sec_404__": True}), encoding="utf-8",
    )
    sec = SECCompanyFactsAdapter()
    payload = await sec.get_submissions_cached(
        "999999", cache_dir=str(cache_dir),
    )
    assert payload is None


@pytest.mark.asyncio
async def test_p2_o3_force_refresh_bypasses_cache(tmp_path) -> None:
    """``force_refresh=True`` always re-hits SEC. With no HTTP context
    open, this raises RuntimeError — confirming the cache was bypassed."""
    import json
    cache_dir = tmp_path / "sec_submissions"
    cache_dir.mkdir()
    (cache_dir / "CIK0000320193.json").write_text(
        json.dumps({"filings": {"recent": {}}, "fiscalYearEnd": "1231"}),
        encoding="utf-8",
    )
    sec = SECCompanyFactsAdapter()
    # No __aenter__ called → client is None → get_submissions raises.
    with pytest.raises(RuntimeError, match="context manager"):
        await sec.get_submissions_cached(
            "320193", cache_dir=str(cache_dir),
            force_refresh=True,
        )


def test_p2_n3_no_cik_yields_null_evidence_url() -> None:
    """Operator hard rule: NULL+evidence > guessing. Without a CIK we
    cannot construct the canonical Archives URL — the events still
    extract, but evidence_url is None on every event."""
    result = _extract_lifecycle_events(_submissions([
        ("25", "2024-03-15", "2024-03-25", "0001353283-24-000111"),
    ]), cik=None)
    assert result["derived_state"] == "delist_effective"
    assert result["form_25_events"][0]["evidence_url"] is None
    assert result["derived_evidence_url"] is None
