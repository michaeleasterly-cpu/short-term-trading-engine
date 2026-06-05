"""Issuer↔security M:N fan-out — the ``ticker_classifications`` →
``issuer_securities`` pure layer (Plan 3 Phase 1).

Spec: ``docs/superpowers/specs/2026-06-04-data-layer-rebuild-design.md`` §4
/ §5.3; corp-history §3.1-§3.4. Handles the share-class fan-out (GOOG +
GOOGL under one Alphabet issuer → one issuer, two securities).

This is the **pure** half of the ``issuer_securities_build`` stage — no
DB, no network. It takes the already-fetched cik-bearing
``ticker_classifications`` rows and produces one ``IssuerSecurityLink`` per
``(issuer_id, classification_id, valid_from=lifetime_start)`` for
``platform.issuer_securities``.

An FMP-only (cik NULL) classification has no SEC issuer (the
``issuer_securities_issuer_fk`` references ``issuers(issuer_id)``, which is
only minted for cik-bearing issuers by ``issuers_build``), so it is
skipped — emitting a link would violate the FK.

The stage handler in ``scripts/ops.py::_stage_issuer_securities_build``
owns the I/O — it SELECTs the cik-bearing classifications, calls
``derive_issuer_securities`` here, and chunk-upserts ``ON CONFLICT
(issuer_id, classification_id, valid_from) DO NOTHING`` (idempotent — a
re-run is a no-op).

The ``issuer_id`` derivation reuses ``issuers_build.mint_issuer_id`` (the
LIVE ``'CIK'+zero-padded-10`` convention) so the link's ``issuer_id``
matches the ``issuers`` PK exactly — no FK miss.
"""
from __future__ import annotations

from datetime import date

import structlog
from pydantic import BaseModel, ConfigDict

from tpcore.identity.issuers_build import mint_issuer_id

logger = structlog.get_logger(__name__)


class SecurityWithCik(BaseModel):
    """One cik-bearing ``ticker_classifications`` row (the input)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    classification_id: str
    cik: str | None = None
    lifetime_start: date
    lifetime_end: date | None = None


class IssuerSecurityLink(BaseModel):
    """One ``platform.issuer_securities`` link ready to UPSERT. Conflict
    target ``(issuer_id, classification_id, valid_from)`` — idempotent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer_id: str
    classification_id: str
    valid_from: date
    valid_to: date | None = None


def derive_issuer_securities(
    securities: list[SecurityWithCik],
) -> list[IssuerSecurityLink]:
    """Derive the M:N issuer↔security links from cik-bearing securities.

    Pure function: no I/O. For each cik-bearing classification, emit one
    link ``(issuer_id=mint_issuer_id(cik), classification_id,
    valid_from=lifetime_start, valid_to=lifetime_end)``. Two share classes
    under the same CIK (GOOG/GOOGL) produce two links to the SAME issuer
    (the fan-out). A cik-NULL (FMP-only) or garbled-cik classification is
    skipped — it has no SEC issuer to link to (FK safety).
    """
    out: list[IssuerSecurityLink] = []
    n_skipped_no_issuer = 0
    for sec in securities:
        issuer_id = mint_issuer_id(sec.cik)
        if issuer_id is None:
            n_skipped_no_issuer += 1
            continue
        out.append(
            IssuerSecurityLink(
                issuer_id=issuer_id,
                classification_id=sec.classification_id,
                valid_from=sec.lifetime_start,
                valid_to=sec.lifetime_end,
            )
        )
    logger.info(
        "issuer_securities.derived",
        n_input=len(securities),
        n_links=len(out),
        n_skipped_no_issuer=n_skipped_no_issuer,
    )
    return out


__all__ = [
    "IssuerSecurityLink",
    "SecurityWithCik",
    "derive_issuer_securities",
]
