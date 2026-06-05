"""Pure-function tests for ``tpcore.identity.issuer_securities_build``.

Hermetic: no DB, no network. Exercises the M:N issuer↔security fan-out
derivation — share classes (GOOG/GOOGL) under one issuer, keyed on
``(issuer_id, classification_id, valid_from)`` for idempotent upsert.
"""
from __future__ import annotations

from datetime import date

from tpcore.identity.issuer_securities_build import (
    IssuerSecurityLink,
    SecurityWithCik,
    derive_issuer_securities,
)


def _s(
    classification_id: str,
    cik: str | None,
    start: date,
    end: date | None = None,
) -> SecurityWithCik:
    return SecurityWithCik(
        classification_id=classification_id,
        cik=cik,
        lifetime_start=start,
        lifetime_end=end,
    )


def test_single_security_link() -> None:
    links = derive_issuer_securities([_s("ID_A", "0000320193", date(1994, 12, 12))])
    assert len(links) == 1
    link = links[0]
    assert isinstance(link, IssuerSecurityLink)
    assert link.issuer_id == "CIK0000320193"
    assert link.classification_id == "ID_A"
    assert link.valid_from == date(1994, 12, 12)
    assert link.valid_to is None


def test_share_class_fanout_one_issuer_two_securities() -> None:
    """GOOG + GOOGL share one Alphabet CIK → one issuer, two
    issuer_securities links (the M:N fan-out)."""
    links = derive_issuer_securities(
        [
            _s("ID_GOOG", "0001652044", date(2014, 4, 3)),
            _s("ID_GOOGL", "0001652044", date(2004, 8, 19)),
        ]
    )
    assert len(links) == 2
    assert {link.issuer_id for link in links} == {"CIK0001652044"}
    assert {link.classification_id for link in links} == {"ID_GOOG", "ID_GOOGL"}


def test_cik_null_security_skipped() -> None:
    """An FMP-only (cik NULL) classification has no SEC issuer → no link
    (the issuer_securities FK references issuers(issuer_id), which only
    exists for cik-bearing issuers)."""
    links = derive_issuer_securities(
        [
            _s("ID_FMP", None, date(2020, 1, 1)),
            _s("ID_SEC", "0000320193", date(1994, 12, 12)),
        ]
    )
    assert len(links) == 1
    assert links[0].classification_id == "ID_SEC"


def test_garbled_cik_skipped() -> None:
    links = derive_issuer_securities([_s("ID_X", "bogus", date(2000, 1, 1))])
    assert links == []


def test_carries_lifetime_end() -> None:
    links = derive_issuer_securities(
        [_s("ID_D", "0000111111", date(2000, 1, 1), date(2010, 1, 1))]
    )
    assert links[0].valid_to == date(2010, 1, 1)


def test_idempotent_deterministic() -> None:
    secs = [
        _s("ID_GOOG", "0001652044", date(2014, 4, 3)),
        _s("ID_GOOGL", "0001652044", date(2004, 8, 19)),
        _s("ID_SEC", "0000320193", date(1994, 12, 12)),
    ]
    a = derive_issuer_securities(secs)
    b = derive_issuer_securities(list(reversed(secs)))
    key = lambda link: (link.issuer_id, link.classification_id, link.valid_from)  # noqa: E731
    assert sorted(a, key=key) == sorted(b, key=key)
