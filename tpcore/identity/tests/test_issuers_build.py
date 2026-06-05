"""Pure-function tests for ``tpcore.identity.issuers_build`` (Plan 3 Phase 1).

Hermetic: no DB, no network. Exercises the pure SEC-submissions → issuer
row + SCD-2 issuer_history assembly. The I/O seam (the bulk reader walk +
chunked upsert) lives in ``scripts/ops.py::_stage_issuers_build`` and is
tested at the stage level.
"""
from __future__ import annotations

from datetime import date

from tpcore.identity.issuers_build import (
    IssuerHistoryRow,
    IssuerRow,
    assemble_issuer,
    mint_issuer_id,
)


def test_mint_issuer_id_convention() -> None:
    """issuer_id = 'CIK' + zero-padded-10 cik (the LIVE convention —
    matches scripts/ops.py::_mint_issuer_id_from_cik, sample CIK0000886158)."""
    assert mint_issuer_id("886158") == "CIK0000886158"
    assert mint_issuer_id("0000886158") == "CIK0000886158"
    assert mint_issuer_id("320193") == "CIK0000320193"


def test_mint_issuer_id_none_on_empty() -> None:
    assert mint_issuer_id(None) is None
    assert mint_issuer_id("") is None
    assert mint_issuer_id("   ") is None


def test_mint_issuer_id_garbled_cik_returns_none() -> None:
    """A non-numeric cik cannot be normalised → None (skip + WARN at stage)."""
    assert mint_issuer_id("not-a-cik") is None


def _payload(
    *,
    name: str = "Apple Inc.",
    former_names: list[dict[str, str]] | None = None,
    fiscal_year_end: str | None = "0930",
    state_of_incorp: str | None = "CA",
    shard_errors: list[str] | None = None,
) -> dict[str, object]:
    p: dict[str, object] = {
        "name": name,
        "formerNames": former_names or [],
        "fiscalYearEnd": fiscal_year_end,
        "stateOfIncorporation": state_of_incorp,
    }
    if shard_errors:
        p["_shard_errors"] = shard_errors
    return p


def test_assemble_issuer_basic_fields() -> None:
    issuer, history = assemble_issuer(
        cik="0000320193",
        payload=_payload(),
        fpfd=date(1994, 12, 12),
        sec_document_type_primary="10-K",
        country_of_incorp="US",
    )
    assert issuer is not None
    assert isinstance(issuer, IssuerRow)
    assert issuer.issuer_id == "CIK0000320193"
    assert issuer.cik == "0000320193"
    assert issuer.legal_name == "Apple Inc."
    assert issuer.fiscal_year_end_month == 9  # "0930" → month 9
    assert issuer.country_of_incorp == "US"
    assert issuer.sec_document_type_primary == "10-K"
    assert issuer.first_public_filing_date == date(1994, 12, 12)
    assert history  # at least the current open row


def test_assemble_issuer_fiscal_year_end_parse() -> None:
    """fiscalYearEnd is MMDD; we keep the MONTH (1-12). Garbled → None."""
    issuer, _ = assemble_issuer(
        cik="1", payload=_payload(fiscal_year_end="1231"),
        fpfd=None, sec_document_type_primary=None, country_of_incorp="US",
    )
    assert issuer is not None
    assert issuer.fiscal_year_end_month == 12

    issuer2, _ = assemble_issuer(
        cik="2", payload=_payload(fiscal_year_end="zz99"),
        fpfd=None, sec_document_type_primary=None, country_of_incorp="US",
    )
    assert issuer2 is not None
    assert issuer2.fiscal_year_end_month is None


def test_assemble_issuer_no_name_skips() -> None:
    """A payload with no usable legal name cannot mint an issuer → None."""
    issuer, history = assemble_issuer(
        cik="1", payload=_payload(name=""),
        fpfd=None, sec_document_type_primary=None, country_of_incorp="US",
    )
    assert issuer is None
    assert history == []


def test_assemble_issuer_garbled_cik_skips() -> None:
    issuer, history = assemble_issuer(
        cik="bogus", payload=_payload(),
        fpfd=None, sec_document_type_primary=None, country_of_incorp="US",
    )
    assert issuer is None
    assert history == []


def test_assemble_issuer_scd2_history_from_former_names() -> None:
    """formerNames → contiguous SCD-2 issuer_history rows, current row last
    with valid_to=None (open). Earliest former-name window starts the chain."""
    former = [
        {"name": "Old Name A", "from": "1994-12-12", "to": "2001-06-01"},
        {"name": "Old Name B", "from": "2001-06-01", "to": "2007-01-09"},
    ]
    issuer, history = assemble_issuer(
        cik="0000320193",
        payload=_payload(name="Apple Inc.", former_names=former),
        fpfd=date(1994, 12, 12),
        sec_document_type_primary="10-K",
        country_of_incorp="US",
    )
    assert issuer is not None
    assert len(history) == 3
    assert all(isinstance(h, IssuerHistoryRow) for h in history)
    # ordered, contiguous, current row open.
    assert history[0].legal_name == "Old Name A"
    assert history[0].valid_from == date(1994, 12, 12)
    assert history[0].valid_to == date(2001, 6, 1)
    assert history[1].legal_name == "Old Name B"
    assert history[1].valid_from == date(2001, 6, 1)
    assert history[1].valid_to == date(2007, 1, 9)
    assert history[2].legal_name == "Apple Inc."
    assert history[2].valid_from == date(2007, 1, 9)
    assert history[2].valid_to is None
    # all carry the same issuer_id + source tag.
    assert {h.issuer_id for h in history} == {"CIK0000320193"}
    assert {h.source for h in history} == {"sec_submissions"}


def test_assemble_issuer_no_former_names_single_open_row() -> None:
    """No formerNames → one open history row anchored at FPFD."""
    issuer, history = assemble_issuer(
        cik="1", payload=_payload(name="Solo Co", former_names=[]),
        fpfd=date(2010, 3, 4), sec_document_type_primary=None,
        country_of_incorp="US",
    )
    assert issuer is not None
    assert len(history) == 1
    assert history[0].legal_name == "Solo Co"
    assert history[0].valid_from == date(2010, 3, 4)
    assert history[0].valid_to is None


def test_assemble_issuer_drops_bad_former_name_date_order() -> None:
    """A former-name window with to <= from (garbled vendor date) is DROPPED
    (the issuer_history valid_to>valid_from order guard) — never emitted."""
    former = [
        {"name": "Bad Window", "from": "2005-01-01", "to": "2004-01-01"},
        {"name": "Good Window", "from": "2006-01-01", "to": "2008-01-01"},
    ]
    issuer, history = assemble_issuer(
        cik="1", payload=_payload(name="Cur Co", former_names=former),
        fpfd=date(2003, 1, 1), sec_document_type_primary=None,
        country_of_incorp="US",
    )
    assert issuer is not None
    names = [h.legal_name for h in history]
    assert "Bad Window" not in names
    assert "Good Window" in names
    assert "Cur Co" in names
    # remaining rows stay strictly ordered with valid_to>valid_from.
    for h in history:
        if h.valid_to is not None:
            assert h.valid_to > h.valid_from


def test_assemble_issuer_skips_former_name_missing_from() -> None:
    """A formerNames entry with no usable 'from' is skipped (no guessing)."""
    former = [{"name": "No From", "from": "", "to": "2009-01-01"}]
    issuer, history = assemble_issuer(
        cik="1", payload=_payload(name="Cur", former_names=former),
        fpfd=date(2000, 1, 1), sec_document_type_primary=None,
        country_of_incorp="US",
    )
    assert issuer is not None
    assert [h.legal_name for h in history] == ["Cur"]
