"""Issuer identity assembler — the SEC-submissions → ``issuers`` +
SCD-2 ``issuer_history`` pure layer (Plan 3 Phase 1).

Spec: ``docs/superpowers/specs/2026-06-04-data-layer-rebuild-design.md`` §4
(identity model) / §5.3 (order); corp-history design §3.1-§3.4.

This is the **pure** half of the ``issuers_build`` stage — no DB, no
network, no asyncpg. It takes one already-fetched SEC merged-submissions
payload (from ``SECSubmissionsBulkReader.get_merged_submissions``) for a
single CIK and produces:

  * one ``IssuerRow`` ready to ``ON CONFLICT (cik)`` upsert into
    ``platform.issuers``;
  * the SCD-2 ``IssuerHistoryRow`` timeline (legal-name / cik over time
    from the SEC ``formerNames`` array) for ``platform.issuer_history``.

The stage handler in ``scripts/ops.py::_stage_issuers_build`` owns the
I/O — it walks every distinct ``cik`` in ``ticker_classifications`` (cik
NOT NULL), reads the bulk submissions for each via the SAME fixed source
``universe_build`` uses, calls ``assemble_issuer`` here, and chunk-upserts
the rows. Mirrors the engine/data symmetry the codebase favours
(``universe_build`` is the precedent).

issuer_id convention (the LIVE convention — verified against
``scripts/ops.py::_mint_issuer_id_from_cik`` + migration ``20260524_1600``;
sample ``CIK0000886158``): ``'CIK' + zero-padded-10 cik``.

Review lessons applied PROACTIVELY (they were blocking bugs in
``universe_build``):
  * **Trust-but-verify SEC shards** — the stage skips a CIK whose payload
    carries ``_shard_errors`` for FPFD (mirrors ``universe_build`` review
    #4); this module never invents a date.
  * **Date-order guards** — a ``formerNames`` window with ``to <= from``
    (garbled vendor date) is DROPPED + WARN here so the
    ``issuer_history`` ``valid_to > valid_from`` ordering invariant holds
    and the DB CHECK never rejects the chunk.
  * **Producer hard-stop / idempotency** live at the stage layer
    (``ON CONFLICT (cik)``); this pure layer is deterministic so a re-run
    produces byte-identical rows.
"""
from __future__ import annotations

from datetime import date

import structlog
from pydantic import BaseModel, ConfigDict, Field

logger = structlog.get_logger(__name__)

ISSUER_HISTORY_SOURCE: str = "sec_submissions"
"""``issuer_history.source`` tag for rows derived from the SEC bulk
submissions ``formerNames`` array."""


class IssuerRow(BaseModel):
    """One issuer ready to UPSERT into ``platform.issuers``.

    ``issuer_id`` is the deterministic ``'CIK'+zero-padded-10`` natural key
    (UPSERT conflict target is ``cik``, the UNIQUE column — review #2
    idempotency: a re-run resolves the SAME row, no duplicate)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer_id: str
    cik: str
    legal_name: str
    country_of_incorp: str | None = None
    fiscal_year_end_month: int | None = Field(default=None, ge=1, le=12)
    sec_document_type_primary: str | None = None
    first_public_filing_date: date | None = None


class IssuerHistoryRow(BaseModel):
    """One SCD-2 ``platform.issuer_history`` row (legal-name / cik over
    time). The half-open window is ``[valid_from, valid_to)``; the current
    (open) row carries ``valid_to=None``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer_id: str
    cik: str
    legal_name: str
    valid_from: date
    valid_to: date | None = None
    source: str = ISSUER_HISTORY_SOURCE


def mint_issuer_id(cik: str | None) -> str | None:
    """Mint the stable ``issuer_id`` from a CIK; ``None`` if no/garbled CIK.

    The LIVE convention (``scripts/ops.py::_mint_issuer_id_from_cik`` +
    migration ``20260524_1600``): strip leading zeros, re-pad to 10,
    prepend ``'CIK'`` — handles both ``'886158'`` and ``'0000886158'``
    deterministically. A non-numeric CIK returns ``None`` (the caller
    skips + WARNs) rather than minting a garbage id.
    """
    if not cik or not str(cik).strip():
        return None
    try:
        return "CIK" + str(int(str(cik).strip())).zfill(10)
    except (ValueError, TypeError):
        return None


def _parse_iso_date(raw: object) -> date | None:
    """Best-effort ISO-date parse; ``None`` on any failure (no guessing)."""
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except (ValueError, TypeError):
        return None


def _fiscal_year_end_month(raw: object) -> int | None:
    """SEC ``fiscalYearEnd`` is ``MMDD`` (e.g. ``'0930'`` → September). Return
    the MONTH (1-12); ``None`` on a garbled value (no guessing)."""
    if not raw:
        return None
    s = str(raw).strip()
    if len(s) < 2 or not s[:2].isdigit():
        return None
    month = int(s[:2])
    if 1 <= month <= 12:
        return month
    return None


def assemble_issuer(
    *,
    cik: str,
    payload: dict[str, object],
    fpfd: date | None,
    sec_document_type_primary: str | None,
    country_of_incorp: str | None,
) -> tuple[IssuerRow | None, list[IssuerHistoryRow]]:
    """Assemble one ``IssuerRow`` + its SCD-2 ``IssuerHistoryRow`` timeline.

    Pure function: no I/O. Given one SEC merged-submissions ``payload``
    (the base JSON from ``SECSubmissionsBulkReader.get_merged_submissions``)
    for ``cik``, derive the issuer row and the contiguous legal-name
    timeline from ``formerNames``.

    Returns ``(None, [])`` when the CIK is garbled or the payload carries no
    usable ``name`` (the issuer cannot be minted — skip + WARN at the
    stage). Otherwise returns the issuer row + ≥1 history row (the open
    current row is always present).

    SCD-2 derivation (corp-history §3.1-§3.4):
      * Each ``formerNames`` entry ``{name, from, to}`` becomes a closed
        history row ``[from, to)``. A window with ``to <= from`` (garbled
        vendor date) is DROPPED + WARN (date-order guard — the
        ``valid_to > valid_from`` invariant).
      * The CURRENT ``name`` becomes the open row, anchored at the latest
        former-name ``to`` (the contiguity boundary) or, when there are no
        former names, at FPFD.
    """
    issuer_id = mint_issuer_id(cik)
    if issuer_id is None:
        logger.warning("issuers_build.garbled_cik_skipped", cik=cik)
        return None, []

    cik_norm = str(int(str(cik).strip())).zfill(10)
    legal_name = str(payload.get("name") or "").strip()
    if not legal_name:
        logger.warning("issuers_build.no_legal_name_skipped", cik=cik_norm)
        return None, []

    fiscal_month = _fiscal_year_end_month(payload.get("fiscalYearEnd"))

    issuer = IssuerRow(
        issuer_id=issuer_id,
        cik=cik_norm,
        legal_name=legal_name,
        country_of_incorp=country_of_incorp,
        fiscal_year_end_month=fiscal_month,
        sec_document_type_primary=sec_document_type_primary,
        first_public_filing_date=fpfd,
    )

    history = _assemble_history(
        issuer_id=issuer_id,
        cik_norm=cik_norm,
        current_name=legal_name,
        former_names=payload.get("formerNames") or [],
        fpfd=fpfd,
    )
    return issuer, history


def _assemble_history(
    *,
    issuer_id: str,
    cik_norm: str,
    current_name: str,
    former_names: object,
    fpfd: date | None,
) -> list[IssuerHistoryRow]:
    """Build the contiguous SCD-2 legal-name timeline (closed former-name
    windows + one open current row)."""
    rows: list[IssuerHistoryRow] = []
    latest_to: date | None = None

    if isinstance(former_names, list):
        for fn in former_names:
            if not isinstance(fn, dict):
                continue
            fn_name = str(fn.get("name") or "").strip()
            fn_from = _parse_iso_date(fn.get("from"))
            fn_to = _parse_iso_date(fn.get("to"))
            if not fn_name or fn_from is None:
                # No usable name/start → skip (no guessing).
                continue
            if fn_to is not None and fn_to <= fn_from:
                # Date-order guard: a window the DB CHECK would reject.
                logger.warning(
                    "issuers_build.bad_former_name_window_dropped",
                    cik=cik_norm,
                    name=fn_name,
                    valid_from=fn_from.isoformat(),
                    valid_to=fn_to.isoformat(),
                )
                continue
            rows.append(
                IssuerHistoryRow(
                    issuer_id=issuer_id,
                    cik=cik_norm,
                    legal_name=fn_name,
                    valid_from=fn_from,
                    valid_to=fn_to,
                )
            )
            if fn_to is not None and (latest_to is None or fn_to > latest_to):
                latest_to = fn_to

    # Anchor the open current row: the latest former-name boundary (contiguity)
    # or FPFD when there are no former names. Falls back to the earliest
    # former-name 'from' if FPFD is unknown and there are no closing dates.
    current_from = latest_to or fpfd
    if current_from is None and rows:
        current_from = min(r.valid_from for r in rows)
    if current_from is None:
        # No FPFD, no former-name dates — emit no history (the stage's
        # producer floor + the issuer row itself still carry the identity;
        # an undated open history row would be a fabricated date).
        logger.warning(
            "issuers_build.no_anchor_for_current_history",
            cik=cik_norm,
            name=current_name,
        )
        return rows
    rows.append(
        IssuerHistoryRow(
            issuer_id=issuer_id,
            cik=cik_norm,
            legal_name=current_name,
            valid_from=current_from,
            valid_to=None,
        )
    )
    return rows


__all__ = [
    "ISSUER_HISTORY_SOURCE",
    "IssuerHistoryRow",
    "IssuerRow",
    "assemble_issuer",
    "mint_issuer_id",
]
